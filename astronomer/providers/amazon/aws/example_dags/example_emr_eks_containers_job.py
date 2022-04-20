import logging
import os
from datetime import datetime, timedelta

import boto3
from airflow import DAG
from airflow.operators.bash import BashOperator
from airflow.operators.python import PythonOperator
from airflow.providers.amazon.aws.operators.emr import EmrContainerOperator
from botocore.exceptions import ClientError

from astronomer.providers.amazon.aws.sensors.emr import EmrContainerSensorAsync

# [START howto_operator_emr_eks_env_variables]
VIRTUAL_CLUSTER_ID = os.getenv("VIRTUAL_CLUSTER_ID", "xxxxxxxx")
AWS_CONN_ID = os.getenv("ASTRO_AWS_CONN_ID", "aws_default")
JOB_ROLE_ARN = os.getenv("JOB_ROLE_ARN", "arn:aws:iam::121212121212:role/test_iam_job_execution_role")
# [END howto_operator_emr_eks_env_variables]

# Job role name and policy name attached to the role
JOB_EXECUTION_ROLE = os.getenv("JOB_EXECUTION_ROLE", "test_iam_job_execution_role")
DEBUGGING_MONITORING_POLICY = os.getenv("DEBUGGING_MONITORING_POLICY", "test_debugging_monitoring_policy")
CONTAINER_SUBMIT_JOB_POLICY = os.getenv(
    "CONTAINER_SUBMIT_JOB_POLICY", "test_emr_container_submit_jobs_policy"
)
JOB_EXECUTION_POLICY = os.getenv("JOB_EXECUTION_POLICY", "test_job_execution_policy")
MANAGE_VIRTUAL_CLUSTERS = os.getenv("MANAGE_VIRTUAL_CLUSTERS", "test_manage_virtual_clusters")

EKS_CONTAINER_PROVIDER_CLUSTER_NAME = os.getenv(
    "EKS_CONTAINER_PROVIDER_CLUSTER_NAME", "providers-team-eks-cluster"
)
KUBECTL_CLUSTER_NAME = os.getenv("KUBECTL_CLUSTER_NAME", "providers-team-eks-namespace")
VIRTUAL_CLUSTER_NAME = os.getenv("EMR_VIRTUAL_CLUSTER_NAME", "providers-team-virtual-eks-cluster")
AWS_ACCESS_KEY_ID = os.getenv("AWS_ACCESS_KEY_ID", "xxxxxxx")
AWS_SECRET_ACCESS_KEY = os.getenv("AWS_SECRET_ACCESS_KEY", "xxxxxxxx")
AWS_DEFAULT_REGION = os.getenv("AWS_DEFAULT_REGION", "us-east-2")
INSTANCE_TYPE = os.getenv("INSTANCE_TYPE", "m4.large")
AIRFLOW_HOME = os.getenv("AIRFLOW_HOME", "/usr/local/airflow")
EXECUTION_TIMEOUT = int(os.getenv("EXECUTION_TIMEOUT", 6))

default_args = {
    "execution_timeout": timedelta(hours=EXECUTION_TIMEOUT),
}


def create_emr_virtual_cluster_func() -> None:
    """Create EMR virtual cluster in container"""
    client = boto3.client("emr-containers")
    try:
        response = client.create_virtual_cluster(
            name=VIRTUAL_CLUSTER_NAME,
            containerProvider={
                "id": EKS_CONTAINER_PROVIDER_CLUSTER_NAME,
                "type": "EKS",
                "info": {"eksInfo": {"namespace": KUBECTL_CLUSTER_NAME}},
            },
        )
        os.environ["VIRTUAL_CLUSTER_ID"] = response["id"]
    except ClientError:
        logging.exception("Error while creating EMR virtual cluster")
        return None


# [START howto_operator_emr_eks_config]
JOB_DRIVER_ARG = {
    "sparkSubmitJobDriver": {
        "entryPoint": "local:///usr/lib/spark/examples/src/main/python/pi.py",
        "sparkSubmitParameters": "--conf spark.executors.instances=2 --conf spark.executors.memory=2G --conf spark.executor.cores=2 --conf spark.driver.cores=1",  # noqa: E501
    }
}

CONFIGURATION_OVERRIDES_ARG = {
    "applicationConfiguration": [
        {
            "classification": "spark-defaults",
            "properties": {
                "spark.hadoop.hive.metastore.client.factory.class": "com.amazonaws.glue.catalog.metastore.AWSGlueDataCatalogHiveClientFactory",  # noqa: E501
            },
        }
    ],
    "monitoringConfiguration": {
        "cloudWatchMonitoringConfiguration": {
            "logGroupName": "/aws/emr-eks-spark",
            "logStreamNamePrefix": "airflow",
        }
    },
}
# [END howto_operator_emr_eks_config]

with DAG(
    dag_id="example_emr_eks_pi_job",
    start_date=datetime(2022, 1, 1),
    schedule_interval=None,
    catchup=False,
    default_args=default_args,
    tags=["example", "async", "emr"],
) as dag:
    # Task steps for DAG to be self-sufficient
    setup_aws_config = BashOperator(
        task_id="setup_aws_config",
        bash_command=f"aws configure set aws_access_key_id {AWS_ACCESS_KEY_ID}; "
        f"aws configure set aws_secret_access_key {AWS_SECRET_ACCESS_KEY}; "
        f"aws configure set default.region {AWS_DEFAULT_REGION}; ",
    )

    # Task to create EMR clusters on EKS
    create_EKS_cluster_kube_namespace_with_role = BashOperator(
        task_id="create_EKS_cluster_kube_namespace_with_role",
        bash_command="sh $AIRFLOW_HOME/dags/example_create_EKS_kube_namespace_with_role.sh ",
    )

    # Task to create EMR virtual cluster
    create_EMR_virtual_cluster = PythonOperator(
        task_id="create_EMR_virtual_cluster",
        python_callable=create_emr_virtual_cluster_func,
    )

    # [START howto_operator_run_emr_container_job]
    run_emr_container_job = EmrContainerOperator(
        task_id="run_emr_container_job",
        virtual_cluster_id=VIRTUAL_CLUSTER_ID,
        execution_role_arn=JOB_ROLE_ARN,
        release_label="emr-6.2.0-latest",
        job_driver=JOB_DRIVER_ARG,
        configuration_overrides=CONFIGURATION_OVERRIDES_ARG,
        name="pi.py",
    )
    # [END howto_operator_emr_eks_jobrun]

    # [START howto_sensor_emr_job_container_sensor]
    emr_job_container_sensor = EmrContainerSensorAsync(
        task_id="emr_job_container_sensor",
        job_id=run_emr_container_job.output,
        virtual_cluster_id=VIRTUAL_CLUSTER_ID,
        poll_interval=5,
        aws_conn_id=AWS_CONN_ID,
    )
    # [END howto_sensor_emr_job_container_sensor]

    # Delete clusters, container providers, role, policy
    remove_clusters_container_role_policy = BashOperator(
        task_id="remove_clusters_container_role_policy",
        bash_command="sh $AIRFLOW_HOME/dags/example_delete_eks_cluster_and_role_policies.sh ",
        trigger_rule="all_done",
    )

    (
        setup_aws_config
        >> create_EKS_cluster_kube_namespace_with_role
        >> create_EMR_virtual_cluster
        >> run_emr_container_job
        >> emr_job_container_sensor
        >> remove_clusters_container_role_policy
    )