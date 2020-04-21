"""
Celery tasks related to AWS for use in the api v2 app.

Note for developers:
If you find yourself adding a new Celery task, please be aware of how Celery
determines which queue to read and write to work on that task. By default,
Celery tasks will go to a queue named "celery". If you wish to separate a task
onto a different queue (which may make it easier to see the volume of specific
waiting tasks), please be sure to update all the relevant configurations to
use that custom queue. This includes CELERY_TASK_ROUTES in config and the
Celery worker's --queues argument (see deployment-configs.yaml in shiftigrade).
"""
import itertools
import json
import logging
from decimal import Decimal

import boto3
from botocore.exceptions import ClientError
from celery import shared_task
from celery.task import task
from dateutil.parser import parse
from django.conf import settings
from django.contrib.auth.models import User
from django.db import IntegrityError, transaction
from django.utils.translation import gettext as _
from rest_framework.exceptions import ValidationError

from api import error_codes
from api.clouds.aws.cloudtrail import (
    extract_ami_tag_events,
    extract_ec2_instance_events,
)
from api.clouds.aws.models import (
    AwsCloudAccount,
    AwsEC2InstanceDefinition,
    AwsInstance,
    AwsMachineImage,
)
from api.clouds.aws.util import (
    create_aws_cloud_account,
    create_aws_machine_image_copy,
    create_initial_aws_instance_events,
    create_new_machine_images,
    generate_aws_ami_messages,
    save_instance,
    save_instance_events,
    save_new_aws_machine_image,
    start_image_inspection,
    update_aws_image_status_error,
    update_aws_image_status_inspected,
    verify_permissions,
)
from api.models import (
    InstanceEvent,
    MachineImage,
)
from api.util import get_standard_cloud_account_name
from util import aws
from util.aws import OPENSHIFT_TAG, RHEL_TAG, is_windows, rewrap_aws_errors
from util.aws.sqs import add_messages_to_queue, read_messages_from_queue
from util.celery import retriable_shared_task
from util.exceptions import (
    AwsECSInstanceNotReady,
    AwsTooManyECSInstances,
    InvalidHoundigradeJsonFormat,
)
from util.misc import generate_device_name

logger = logging.getLogger(__name__)

# Constants
CLOUD_KEY = "cloud"
CLOUD_TYPE_AWS = "aws"


@retriable_shared_task(autoretry_for=(RuntimeError,))
@rewrap_aws_errors
def configure_customer_aws_and_create_cloud_account(
    username, customer_arn, authentication_id, application_id, endpoint_id, source_id
):
    """
    Configure the customer's AWS account and create our CloudAccount.

    This function is decorated to retry if an unhandled `RuntimeError` is
    raised, which is the exception we raise in `rewrap_aws_errors` if we
    encounter an unexpected error from AWS. This means it should keep retrying
    if AWS is misbehaving.

    Args:
        username (string): Username of the user that will own the new cloud account
        customer_arn (str): customer's ARN
        authentication_id (str): Platform Sources' Authentication object id
        application_id (str): Platform Sources' Application object id
        endpoint_id (str): Platform Sources' Endpoint object id
        source_id (str): Platform Sources' Source object id
    """
    try:
        user = User.objects.get(username=username)
    except User.DoesNotExist:
        error = error_codes.CG1000
        error.log_internal_message(
            logger, {"application_id": application_id, "username": username}
        )
        error.notify(username, application_id)

        return
    customer_aws_account_id = aws.AwsArn(customer_arn).account_id

    cloud_account_name = get_standard_cloud_account_name("aws", customer_aws_account_id)
    create_aws_cloud_account(
        user,
        customer_arn,
        cloud_account_name,
        authentication_id,
        application_id,
        endpoint_id,
        source_id,
    )


@retriable_shared_task
@rewrap_aws_errors
def initial_aws_describe_instances(account_id):
    """
    Fetch and save instances data found upon AWS cloud account creation.

    Args:
        account_id (int): the AwsAccount id
    """
    try:
        aws_account = AwsCloudAccount.objects.get(pk=account_id)
    except AwsCloudAccount.DoesNotExist:
        logger.warning(
            _("AwsCloudAccount id %s could not be found for initial describe"),
            account_id,
        )
        # This can happen if a customer creates and then quickly deletes their
        # cloud account before this async task has started to run. Early exit!
        return

    account = aws_account.cloud_account.get()
    if not account.is_enabled:
        logger.warning(
            _("AwsCloudAccount id %s is not enabled; skipping initial describe"),
            account_id,
        )
        # This can happen if a customer creates and then quickly disabled their
        # cloud account before this async task has started to run. Early exit!
        return
    arn = aws_account.account_arn

    session = aws.get_session(arn)
    instances_data = aws.describe_instances_everywhere(session)
    with transaction.atomic():
        try:
            AwsCloudAccount.objects.get(pk=account_id)
            new_ami_ids = create_new_machine_images(session, instances_data)
            create_initial_aws_instance_events(account, instances_data)
        except AwsCloudAccount.DoesNotExist:
            logger.warning(
                _(
                    "AwsCloudAccount id %s could not be found to save newly "
                    "discovered images and instances"
                ),
                account_id,
            )
            # This can happen if a customer deleted their cloud account between
            # the start of this function and here. The AWS calls for
            # describe_instances_everywhere may be slow and are not within this
            # transaction. That's why we have to check again after it.
            return

    messages = generate_aws_ami_messages(instances_data, new_ami_ids)
    for message in messages:
        start_image_inspection(str(arn), message["image_id"], message["region"])


@retriable_shared_task  # noqa: C901
@rewrap_aws_errors
def copy_ami_snapshot(arn, ami_id, snapshot_region, reference_ami_id=None):
    """
    Copy an AWS Snapshot to the primary AWS account.

    Args:
        arn (str): The AWS Resource Number for the account with the snapshot
        ami_id (str): The AWS ID for the machine image
        snapshot_region (str): The region the snapshot resides in
        reference_ami_id (str): Optional. The id of the original image from
            which this image was copied. We need to know this in some cases
            where we create a copy of the image in the customer's account
            before we can copy its snapshot, and we must pass this information
            forward for appropriate reference and cleanup.

    Returns:
        None: Run as an asynchronous Celery task.

    """
    if reference_ami_id:
        if not AwsMachineImage.objects.filter(ec2_ami_id=reference_ami_id).exists():
            logger.warning(
                _(
                    "AwsMachineImage with EC2 AMI ID %(ami_id)s could not be "
                    "found for copy_ami_snapshot (using reference_ami_id)."
                ),
                {"ami_id": ami_id},
            )
            return
    elif not AwsMachineImage.objects.filter(ec2_ami_id=ami_id).exists():
        logger.warning(
            _(
                "AwsMachineImage with EC2 AMI ID %(ami_id)s could not be "
                "found for copy_ami_snapshot."
            ),
            {"ami_id": ami_id},
        )
        return

    session = aws.get_session(arn)
    session_account_id = aws.get_session_account_id(session)
    ami = aws.get_ami(session, ami_id, snapshot_region)
    if not ami:
        logger.info(
            _(
                "Cannot copy AMI %(image_id)s snapshot from "
                "%(source_region)s. Saving ERROR status."
            ),
            {"image_id": ami_id, "source_region": snapshot_region},
        )
        update_aws_image_status_error(ami_id)
        return

    customer_snapshot_id = aws.get_ami_snapshot_id(ami)
    if not customer_snapshot_id:
        logger.info(
            _(
                "Cannot get customer snapshot id from AMI %(image_id)s "
                "in %(source_region)s. Saving ERROR status."
            ),
            {"image_id": ami_id, "source_region": snapshot_region},
        )
        update_aws_image_status_error(ami_id)
        return
    try:
        customer_snapshot = aws.get_snapshot(
            session, customer_snapshot_id, snapshot_region
        )

        if customer_snapshot.encrypted:
            awsimage = AwsMachineImage.objects.get(ec2_ami_id=ami_id)
            image = awsimage.machine_image.get()
            image.is_encrypted = True
            image.status = image.ERROR
            image.save()
            logger.info(
                _(
                    'AWS snapshot "%(snapshot_id)s" for image "%(image_id)s" '
                    'found using customer ARN "%(arn)s" is encrypted and '
                    "cannot be copied."
                ),
                {
                    "snapshot_id": customer_snapshot.snapshot_id,
                    "image_id": ami.id,
                    "arn": arn,
                },
            )
            return

        logger.info(
            _(
                'AWS snapshot "%(snapshot_id)s" for image "%(image_id)s" has '
                'owner "%(owner_id)s"; current session is account '
                '"%(account_id)s"'
            ),
            {
                "snapshot_id": customer_snapshot.snapshot_id,
                "image_id": ami.id,
                "owner_id": customer_snapshot.owner_id,
                "account_id": session_account_id,
            },
        )

        if (
            customer_snapshot.owner_id != session_account_id
            and reference_ami_id is None
        ):
            copy_ami_to_customer_account.delay(arn, ami_id, snapshot_region)
            # Early return because we need to stop processing the current AMI.
            # A future call will process this new copy of the
            # current AMI instead.
            return
    except ClientError as e:
        if e.response.get("Error").get("Code") == "InvalidSnapshot.NotFound":
            # Possibly a marketplace AMI, try to handle it by copying.
            copy_ami_to_customer_account.delay(arn, ami_id, snapshot_region)
            return
        raise e

    aws.add_snapshot_ownership(customer_snapshot)

    snapshot_copy_id = aws.copy_snapshot(customer_snapshot_id, snapshot_region)
    logger.info(
        _(
            "%(label)s: customer_snapshot_id=%(snapshot_id)s, "
            "snapshot_copy_id=%(copy_id)s"
        ),
        {
            "label": "copy_ami_snapshot",
            "snapshot_id": customer_snapshot_id,
            "copy_id": snapshot_copy_id,
        },
    )

    # Schedule removal of ownership on customer snapshot
    remove_snapshot_ownership.delay(
        arn, customer_snapshot_id, snapshot_region, snapshot_copy_id
    )

    if reference_ami_id is not None:
        # If a reference ami exists, that means we have been working with a
        # copy in here. That means we need to remove that copy and pass the
        # original reference AMI ID through the rest of the task chain so the
        # results get reported for that original reference AMI, not our copy.
        # TODO FIXME Do we or don't we clean up?
        # If we do, we need permissions to include `ec2:DeregisterImage` and
        # `ec2:DeleteSnapshot` but those are both somewhat scary...
        #
        # For now, since we are not deleting the copy image from AWS, we need
        # to record a reference to our database that we can look at later to
        # indicate the relationship between the original AMI and the copy.
        create_aws_machine_image_copy(ami_id, reference_ami_id)
        ami_id = reference_ami_id

    # Create volume from snapshot copy
    create_volume.delay(ami_id, snapshot_copy_id)


@retriable_shared_task
@rewrap_aws_errors
def copy_ami_to_customer_account(arn, reference_ami_id, snapshot_region):
    """
    Copy an AWS Image to the customer's AWS account.

    This is an intermediate step that we occasionally need to use when the
    customer has an instance based on a image that has been privately shared
    by a third party, and that means we cannot directly copy its snapshot. We
    can, however, create a copy of the image in the customer's account and use
    that copy for the remainder of the inspection process.

    Args:
        arn (str): The AWS Resource Number for the account with the snapshot
        reference_ami_id (str): The AWS ID for the original image to copy
        snapshot_region (str): The region the snapshot resides in

    Returns:
        None: Run as an asynchronous Celery task.

    """
    if not AwsMachineImage.objects.filter(ec2_ami_id=reference_ami_id).exists():
        logger.warning(
            _(
                "AwsMachineImage with EC2 AMI ID %(reference_ami_id)s could "
                "not be found for copy_ami_to_customer_account."
            ),
            {"reference_ami_id": reference_ami_id},
        )
        return

    session = aws.get_session(arn)
    reference_ami = aws.get_ami(session, reference_ami_id, snapshot_region)
    if not reference_ami:
        logger.info(
            _(
                "Cannot copy reference AMI %(image_id)s from "
                "%(source_region)s. Saving ERROR status."
            ),
            {"image_id": reference_ami_id, "source_region": snapshot_region},
        )
        update_aws_image_status_error(reference_ami_id)
        return

    try:
        new_ami_id = aws.copy_ami(session, reference_ami.id, snapshot_region)
    except ClientError as e:
        public_errors = (
            "Images from AWS Marketplace cannot be copied to another AWS account",
            "Images with EC2 BillingProduct codes cannot be copied to another "
            "AWS account",
            "You do not have permission to access the storage of this ami",
        )
        private_errors = "You do not have permission to access the storage of this ami"
        if e.response.get("Error").get("Code") == "InvalidRequest":
            error = (
                e.response.get("Error").get("Message")[:-1]
                if e.response.get("Error").get("Message").endswith(".")
                else e.response.get("Error").get("Message")
            )

            if not reference_ami.public and error in private_errors:
                # This appears to be a private AMI, shared with our customer,
                # but not given access to the storage.
                logger.warning(
                    _(
                        'Found a private image "%s" with inaccessible '
                        "storage. Saving ERROR status."
                    ),
                    reference_ami_id,
                )
                update_aws_image_status_error(reference_ami_id)
                return
            elif error in public_errors:
                # This appears to be a marketplace AMI, mark it as inspected.
                logger.info(
                    _(
                        'Found a marketplace/community image "%s", '
                        "marking as inspected"
                    ),
                    reference_ami_id,
                )
                update_aws_image_status_inspected(
                    ec2_ami_id=reference_ami_id, aws_marketplace_image=True
                )
                return

        raise e

    copy_ami_snapshot.delay(arn, new_ami_id, snapshot_region, reference_ami_id)


@retriable_shared_task
@rewrap_aws_errors
def remove_snapshot_ownership(
    arn, customer_snapshot_id, customer_snapshot_region, snapshot_copy_id
):
    """
    Remove cloudigrade ownership from customer snapshot.

    Args:
        arn (str): The AWS Resource Number for the account with the snapshot
        customer_snapshot_id (str): The id of the snapshot to remove ownership
        customer_snapshot_region (str): The region where
            customer_snapshot_id resides
        snapshot_copy_id (str): The id of the snapshot that must
            be ready to continue
    Returns:
        None: Run as an asynchronous Celery task.
    """
    ec2 = boto3.resource("ec2")

    # Wait for snapshot to be ready
    try:
        snapshot_copy = ec2.Snapshot(snapshot_copy_id)
        aws.check_snapshot_state(snapshot_copy)
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") == "InvalidSnapshot.NotFound":
            logger.info(
                _(
                    "%(label)s detected snapshot_copy_id %(copy_id)s "
                    "already deleted."
                ),
                {"label": "remove_snapshot_ownership", "copy_id": snapshot_copy_id},
            )
        else:
            raise

    # Remove permissions from customer_snapshot
    logger.info(
        _("%(label)s remove ownership from customer snapshot %(snapshot_id)s"),
        {"label": "remove_snapshot_ownership", "snapshot_id": customer_snapshot_id},
    )
    session = aws.get_session(arn)
    customer_snapshot = aws.get_snapshot(
        session, customer_snapshot_id, customer_snapshot_region
    )
    aws.remove_snapshot_ownership(customer_snapshot)


@retriable_shared_task
@rewrap_aws_errors
def create_volume(ami_id, snapshot_copy_id):
    """
    Create an AWS Volume in the primary AWS account.

    Args:
        ami_id (str): The AWS AMI id for which this request originated
        snapshot_copy_id (str): The id of the snapshot to use for the volume
    Returns:
        None: Run as an asynchronous Celery task.
    """
    zone = settings.HOUNDIGRADE_AWS_AVAILABILITY_ZONE
    volume_id = aws.create_volume(snapshot_copy_id, zone)
    region = aws.get_region_from_availability_zone(zone)

    logger.info(
        _("%(label)s: volume_id=%(volume_id)s, volume_region=%(region)s"),
        {"label": "create_volume", "volume_id": volume_id, "region": region},
    )

    delete_snapshot.delay(snapshot_copy_id, volume_id, region)
    enqueue_ready_volume.delay(ami_id, volume_id, region)


@retriable_shared_task
@rewrap_aws_errors
def delete_snapshot(snapshot_copy_id, volume_id, volume_region):
    """
    Delete snapshot after volume is ready.

    Args:
        snapshot_copy_id (str): The id of the snapshot to delete
        volume_id (str): The id of the volume that must be ready
        volume_region (str): The region of the volume
    Returns:
        None: Run as an asynchronous Celery task.
    """
    ec2 = boto3.resource("ec2")

    # Wait for volume to be ready
    volume = aws.get_volume(volume_id, volume_region)
    aws.check_volume_state(volume)

    # Delete snapshot_copy
    logger.info(
        _("%(label)s delete cloudigrade snapshot copy %(copy_id)s"),
        {"label": "delete_snapshot", "copy_id": snapshot_copy_id},
    )
    snapshot_copy = ec2.Snapshot(snapshot_copy_id)
    snapshot_copy.delete(DryRun=False)


@retriable_shared_task
@rewrap_aws_errors
def enqueue_ready_volume(ami_id, volume_id, volume_region):
    """
    Enqueues information about an AMI and volume for later use.

    Args:
        ami_id (str): The AWS AMI id for which this request originated
        volume_id (str): The id of the volume that must be ready
        volume_region (str): The region of the volume
    Returns:
        None: Run as an asynchronous Celery task.
    """
    volume = aws.get_volume(volume_id, volume_region)
    aws.check_volume_state(volume)
    messages = [{"ami_id": ami_id, "volume_id": volume_id}]

    queue_name = "{0}ready_volumes".format(settings.AWS_NAME_PREFIX)
    add_messages_to_queue(queue_name, messages)


@retriable_shared_task
@rewrap_aws_errors
def scale_down_cluster():
    """
    Scale down cluster after houndigrade scan.

    Returns:
        None: Run as an asynchronous Celery task.

    """
    logger.info(_("Scaling down ECS cluster."))
    aws.scale_down(settings.HOUNDIGRADE_AWS_AUTOSCALING_GROUP_NAME)


@shared_task
@rewrap_aws_errors
def scale_up_inspection_cluster():
    """
    Scale up the "houndigrade" inspection cluster.

    Returns:
        None: Run as a scheduled Celery task.

    """
    queue_name = "{0}ready_volumes".format(settings.AWS_NAME_PREFIX)
    scaled_down, auto_scaling_group = aws.is_scaled_down(
        settings.HOUNDIGRADE_AWS_AUTOSCALING_GROUP_NAME
    )
    if not scaled_down:
        # Quietly exit and let a future run check the scaling.
        args = {
            "name": settings.HOUNDIGRADE_AWS_AUTOSCALING_GROUP_NAME,
            "min_size": auto_scaling_group.get("MinSize"),
            "max_size": auto_scaling_group.get("MinSize"),
            "desired_capacity": auto_scaling_group.get("DesiredCapacity"),
            "len_instances": len(auto_scaling_group.get("Instances", [])),
        }
        logger.info(
            _(
                'Auto Scaling group "%(name)s" is not scaled down. '
                "MinSize=%(min_size)s MaxSize=%(max_size)s "
                "DesiredCapacity=%(desired_capacity)s "
                "len(Instances)=%(len_instances)s"
            ),
            args,
        )
        for instance in auto_scaling_group.get("Instances", []):
            logger.info(_("Instance exists: %s"), instance.get("InstanceId"))
        return

    messages = read_messages_from_queue(
        queue_name, settings.HOUNDIGRADE_AWS_VOLUME_BATCH_SIZE
    )

    if len(messages) == 0:
        # Quietly exit and let a future run check for messages.
        logger.info(_("Not scaling up because no new volumes were found."))
        return

    try:
        aws.scale_up(settings.HOUNDIGRADE_AWS_AUTOSCALING_GROUP_NAME)
    except ClientError:
        # If scale_up fails unexpectedly, requeue messages so they aren't lost.
        add_messages_to_queue(queue_name, messages)
        raise

    run_inspection_cluster.delay(messages)


@retriable_shared_task  # noqa: C901
@rewrap_aws_errors
def run_inspection_cluster(messages, cloud="aws"):
    """
    Run task definition for "houndigrade" on the cluster.

    Args:
        messages (list): A list of dictionary items containing
            meta-data (ami_id, volume_id)
        cloud (str): String key representing what cloud we're inspecting.

    Returns:
        None: Run as an asynchronous Celery task.

    """
    relevant_messages = []
    for message in messages:
        try:
            ec2_ami_id = message.get("ami_id")
            aws_machine_image = AwsMachineImage.objects.get(ec2_ami_id=ec2_ami_id)
            machine_image = aws_machine_image.machine_image.get()
            machine_image.status = MachineImage.INSPECTING
            machine_image.save()
            relevant_messages.append(message)
        except AwsMachineImage.DoesNotExist:
            logger.warning(
                _(
                    "Skipping inspection because we do not have an "
                    "AwsMachineImage for %(ec2_ami_id)s (%(message)s)"
                ),
                {"ec2_ami_id": ec2_ami_id, "message": message},
            )
        except MachineImage.DoesNotExist:
            logger.warning(
                _(
                    "Skipping inspection because we do not have a "
                    "MachineImage for %(ec2_ami_id)s (%(message)s)"
                ),
                {"ec2_ami_id": ec2_ami_id, "message": message},
            )

    if not relevant_messages:
        # Early return if nothing actually needs inspection.
        # The cluster has likely scaled up now and needs to scale down.
        scale_down_cluster.delay()
        return

    task_command = ["-c", cloud]
    if settings.HOUNDIGRADE_DEBUG:
        task_command.extend(["--debug"])

    ecs = boto3.client("ecs")
    # get ecs container instance id
    result = ecs.list_container_instances(cluster=settings.HOUNDIGRADE_ECS_CLUSTER_NAME)

    # verify we have our single container instance
    num_instances = len(result["containerInstanceArns"])
    if num_instances == 0:
        raise AwsECSInstanceNotReady
    elif num_instances > 1:
        raise AwsTooManyECSInstances

    result = ecs.describe_container_instances(
        containerInstances=[result["containerInstanceArns"][0]],
        cluster=settings.HOUNDIGRADE_ECS_CLUSTER_NAME,
    )
    ec2_instance_id = result["containerInstances"][0]["ec2InstanceId"]

    # Obtain boto EC2 Instance
    ec2 = boto3.resource("ec2")
    ec2_instance = ec2.Instance(ec2_instance_id)
    current_state = ec2_instance.state
    if not aws.InstanceState.is_running(current_state["Code"]):
        logger.warning(
            _(
                "ECS cluster %(cluster)s has container instance %(ec2_instance_id)s "
                "but instance state is %(current_state)s"
            ),
            {
                "cluster": settings.HOUNDIGRADE_ECS_CLUSTER_NAME,
                "ec2_instance_id": ec2_instance_id,
                "current_state": current_state,
            },
        )
        raise AwsECSInstanceNotReady

    logger.info(_("%s attaching volumes"), "run_inspection_cluster")
    # attach volumes
    for index, message in enumerate(relevant_messages):
        ec2_ami_id = message["ami_id"]
        ec2_volume_id = message["volume_id"]
        mount_point = generate_device_name(index)
        volume = ec2.Volume(ec2_volume_id)
        logger.info(
            _(
                "%(label)s attaching volume %(volume_id)s from AMI "
                "%(ami_id)s to instance %(instance)s at %(mount_point)s"
            ),
            {
                "label": "run_inspection_cluster",
                "volume_id": ec2_volume_id,
                "ami_id": ec2_ami_id,
                "instance": ec2_instance_id,
                "mount_point": mount_point,
            },
        )
        try:
            volume.attach_to_instance(Device=mount_point, InstanceId=ec2_instance_id)
        except ClientError as e:
            error_code = e.response.get("Error").get("Code")
            error_message = e.response.get("Error").get("Message")

            if (
                error_code in ("OptInRequired", "IncorrectInstanceState")
                and "marketplace" in error_message.lower()
            ):
                logger.info(
                    _(
                        'Found a marketplace AMI "%s" when trying to copy '
                        "volume. This should not happen, but here we are."
                    ),
                    ec2_ami_id,
                )
                save_success = update_aws_image_status_inspected(ec2_ami_id)
            else:
                logger.error(
                    _(
                        "Encountered an issue when trying to attach "
                        'volume "%(volume_id)s" from AMI "%(ami_id)s" '
                        "to inspection instance. Error code: "
                        '"%(error_code)s". Error message: '
                        '"%(error_message)s". Setting image state to '
                        "ERROR."
                    ),
                    {
                        "volume_id": ec2_volume_id,
                        "ami_id": ec2_ami_id,
                        "error_code": error_code,
                        "error_message": error_message,
                    },
                )
                save_success = update_aws_image_status_error(ec2_ami_id)

            if not save_success:
                logger.warning(
                    _(
                        "Failed to save updated status to AwsMachineImage for "
                        "EC2 AMI ID %(ec2_ami_id)s in run_inspection_cluster"
                    ),
                    {"ec2_ami_id": ec2_ami_id},
                )

            volume.delete()
            continue

        logger.info(
            _("%(label)s modify volume %(volume_id)s to auto-delete"),
            {"label": "run_inspection_cluster", "volume_id": ec2_volume_id},
        )
        # Configure volumes to delete when instance is scaled down
        ec2_instance.modify_attribute(
            BlockDeviceMappings=[
                {"DeviceName": mount_point, "Ebs": {"DeleteOnTermination": True},}
            ]
        )

        task_command.extend(["-t", message["ami_id"], mount_point])

    if "-t" not in task_command:
        logger.warning(_("No targets left to inspect, exiting early."))
        return

    result = ecs.register_task_definition(
        family=f"{settings.HOUNDIGRADE_ECS_FAMILY_NAME}",
        containerDefinitions=[_build_container_definition(task_command)],
        requiresCompatibilities=["EC2"],
    )

    # release the hounds
    ecs.run_task(
        cluster=settings.HOUNDIGRADE_ECS_CLUSTER_NAME,
        taskDefinition=result["taskDefinition"]["taskDefinitionArn"],
    )


def _build_container_definition(task_command):
    """
    Build a container definition to be used by an ecs task.

    Args:
        task_command (list): Command to insert into the definition.

    Returns (dict): Complete container definition.

    """
    container_definition = {
        "name": "Houndigrade",
        "image": f"{settings.HOUNDIGRADE_ECS_IMAGE_NAME}:"
        f"{settings.HOUNDIGRADE_ECS_IMAGE_TAG}",
        "cpu": 0,
        "memoryReservation": 256,
        "essential": True,
        "command": task_command,
        "environment": [
            {"name": "AWS_DEFAULT_REGION", "value": settings.AWS_SQS_REGION},
            {"name": "AWS_ACCESS_KEY_ID", "value": settings.AWS_SQS_ACCESS_KEY_ID},
            {
                "name": "AWS_SECRET_ACCESS_KEY",
                "value": settings.AWS_SQS_SECRET_ACCESS_KEY,
            },
            {
                "name": "RESULTS_QUEUE_NAME",
                "value": settings.HOUNDIGRADE_RESULTS_QUEUE_NAME,
            },
            {"name": "EXCHANGE_NAME", "value": settings.HOUNDIGRADE_EXCHANGE_NAME},
            {"name": "QUEUE_CONNECTION_URL", "value": settings.CELERY_BROKER_URL},
        ],
        "privileged": True,
        "logConfiguration": {
            "logDriver": "awslogs",
            "options": {
                "awslogs-create-group": "true",
                "awslogs-group": f"{settings.AWS_NAME_PREFIX}cloudigrade-ecs",
                "awslogs-region": settings.AWS_SQS_REGION,
            },
        },
    }
    if settings.HOUNDIGRADE_ENABLE_SENTRY:
        container_definition["environment"].extend(
            [
                {
                    "name": "HOUNDIGRADE_SENTRY_DSN",
                    "value": settings.HOUNDIGRADE_SENTRY_DSN,
                },
                {
                    "name": "HOUNDIGRADE_SENTRY_RELEASE",
                    "value": settings.HOUNDIGRADE_SENTRY_RELEASE,
                },
                {
                    "name": "HOUNDIGRADE_SENTRY_ENVIRONMENT",
                    "value": settings.HOUNDIGRADE_SENTRY_ENVIRONMENT,
                },
            ]
        )

    return container_definition


@transaction.atomic
def persist_aws_inspection_cluster_results(inspection_results):
    """
    Persist the aws houndigrade inspection result.

    Args:
        inspection_results (dict): A dict containing houndigrade results
    Returns:
        None
    """
    images = inspection_results.get("images")
    if images is None:
        raise InvalidHoundigradeJsonFormat(
            _("Inspection results json missing images: {}").format(inspection_results)
        )

    for image_id, image_json in images.items():
        save_success = update_aws_image_status_inspected(
            image_id, inspection_json=json.dumps(image_json)
        )
        if not save_success:
            logger.warning(
                _(
                    "Persisting AWS inspection results for EC2 AMI ID "
                    "%(ec2_ami_id)s, but we do not have any record of it. "
                    "Inspection results are: %(inspection_json)s"
                ),
                {"ec2_ami_id": image_id, "inspection_json": image_json},
            )


@shared_task
@rewrap_aws_errors
def analyze_log():
    """Read SQS Queue for log location, and parse log for events."""
    queue_url = settings.CLOUDTRAIL_EVENT_URL
    successes, failures = [], []
    for message in aws.yield_messages_from_queue(queue_url):
        success = False
        try:
            success = _process_cloudtrail_message(message)
        except Exception as e:
            logger.exception(_("Unexpected error in log processing: %s"), e)
        if success:
            logger.info(
                _("Successfully processed message id %s; deleting from queue."),
                message.message_id,
            )
            aws.delete_messages_from_queue(queue_url, [message])
            successes.append(message)
        else:
            logger.error(
                _("Failed to process message id %s; leaving on queue."),
                message.message_id,
            )
            logger.debug(_("Failed message body is: %s"), message.body)
            failures.append(message)
    return successes, failures


def _process_cloudtrail_message(message):
    """
    Process a single CloudTrail log update's SQS message.

    This may have the side-effect of starting the inspection process for newly
    discovered images.

    Args:
        message (Message): the SQS Message object to process

    Returns:
        bool: True only if message processing completed without error.

    """
    logs = []
    extracted_messages = aws.extract_sqs_message(message)

    # Get the S3 objects referenced by the SQS messages
    for extracted_message in extracted_messages:
        bucket = extracted_message["bucket"]["name"]
        key = extracted_message["object"]["key"]
        raw_content = aws.get_object_content_from_s3(bucket, key)
        content = json.loads(raw_content)
        logs.append((content, bucket, key))
        logger.debug(
            _("Read CloudTrail log file from bucket %(bucket)s object key %(key)s"),
            {"bucket": bucket, "key": key},
        )

    # Extract actionable details from each of the S3 log files
    instance_events = []
    ami_tag_events = []
    for content, bucket, key in logs:
        for record in content.get("Records", []):
            instance_events.extend(extract_ec2_instance_events(record))
            ami_tag_events.extend(extract_ami_tag_events(record))

    # Get supporting details from AWS so we can save our models.
    # Note: It's important that we do all AWS API loading calls here before
    # saving anything to the database. We don't want to leave database write
    # transactions open while waiting on external APIs.
    described_instances = _load_missing_instance_data(instance_events)
    described_amis = _load_missing_ami_data(instance_events, ami_tag_events)

    try:
        # Save the results
        new_images = _save_cloudtrail_activity(
            instance_events, ami_tag_events, described_instances, described_amis,
        )
        # Starting image inspection MUST come after all other database writes
        # so that we are confident the atomic transaction will complete.
        for ami_id, awsimage in new_images.items():
            # Is it even possible to get here when status is *not* PENDING?
            # I don't think so, but just in case, we only want inspection to
            # start if status == PENDING.
            image = awsimage.machine_image.get()
            if image.status == image.PENDING:
                start_image_inspection(
                    described_amis[ami_id]["found_by_account_arn"],
                    ami_id,
                    described_amis[ami_id]["found_in_region"],
                )

        logger.debug(_("Saved instances and/or events to the DB."))
        return True
    except:  # noqa: E722 because we don't know what could go wrong yet.
        logger.exception(
            _(
                "Failed to save instances and/or events to the DB. "
                "Instance events: %(instance_events)s AMI tag events: "
                "%(ami_tag_events)s"
            ),
            {"instance_events": instance_events, "ami_tag_events": ami_tag_events},
        )
        return False


def _load_missing_instance_data(instance_events):  # noqa: C901
    """
    Load additional data so we can create instances from Cloud Trail events.

    We only get the necessary instance type and AMI ID from AWS Cloud Trail for
    the "RunInstances" event upon first creation of an instance. If we didn't
    get that (for example, if an instance already existed but was stopped when
    we got the cloud account), that means we may not know about the instance
    and need to describe it before we create our record of it.

    However, there is an edge-case possibility when AWS gives us events out of
    order. If we receive an instance event *before* its "RunInstances" that
    would fully describe it, we have to describe it now even though the later
    event should eventually give us that same information. There's a super edge
    case here that means the AWS user could have also changed the type before
    we receive that initial "RunInstances" event, but we are not explicitly
    handling that scenario as of this writing.

    Args:
        instance_events (list[CloudTrailInstanceEvent]): found instance events

    Side-effect:
        instance_events input argument may have been updated with missing
            image_id values from AWS.

    Returns:
         dict: dict of dicts from AWS describing EC2 instances that are
            referenced by the input arguments but are not present in our
            database, with the outer key being each EC2 instance's ID.

    """
    all_ec2_instance_ids = set(
        [instance_event.ec2_instance_id for instance_event in instance_events]
    )
    described_instances = dict()
    defined_ec2_instance_ids = set()
    # First identify which instances DON'T need to be described because we
    # either already have them stored or at least one of instance_events has
    # enough information for it.
    for instance_event in instance_events:
        ec2_instance_id = instance_event.ec2_instance_id
        if (
            _instance_event_is_complete(instance_event)
            or ec2_instance_id in defined_ec2_instance_ids
        ):
            # This means the incoming data is sufficiently populated so we
            # should know the instance's image and type.
            defined_ec2_instance_ids.add(ec2_instance_id)
        elif (
            AwsInstance.objects.filter(
                ec2_instance_id=instance_event.ec2_instance_id,
                instance__machine_image__isnull=False,
            ).exists()
            and InstanceEvent.objects.filter(
                instance__aws_instance__ec2_instance_id=ec2_instance_id,
                aws_instance_event__instance_type__isnull=False,
            ).exists()
        ):
            # This means we already know the instance's image and at least once
            # we have known the instance's type from an event.
            defined_ec2_instance_ids.add(ec2_instance_id)

    # Iterate through the instance events grouped by account and region in
    # order to minimize the number of sessions and AWS API calls.
    for (aws_account_id, region), grouped_instance_events in itertools.groupby(
        instance_events, key=lambda e: (e.aws_account_id, e.region)
    ):
        grouped_instance_events = list(grouped_instance_events)
        # Find the set of EC2 instance IDs that belong to this account+region.
        ec2_instance_ids = set(
            [e.ec2_instance_id for e in grouped_instance_events]
        ).difference(defined_ec2_instance_ids)

        if not ec2_instance_ids:
            # Early continue if there are no instances we need to describe!
            continue

        awsaccount = AwsCloudAccount.objects.get(aws_account_id=aws_account_id)
        session = aws.get_session(awsaccount.account_arn, region)

        # Get all relevant instances in one API call for this account+region.
        new_described_instances = aws.describe_instances(
            session, ec2_instance_ids, region
        )

        # How we found these instances will be important to save *later*.
        # This wouldn't be necessary if we could save these here, but we don't
        # want to mix DB transactions with external AWS API calls.
        for (ec2_instance_id, described_instance) in new_described_instances.items():
            logger.info(
                _(
                    "Loading data for EC2 Instance %(ec2_instance_id)s for "
                    "ARN %(account_arn)s in region %(region)s"
                ),
                {
                    "ec2_instance_id": ec2_instance_id,
                    "account_arn": awsaccount.account_arn,
                    "region": region,
                },
            )
            described_instance["found_by_account_arn"] = awsaccount.account_arn
            described_instance["found_in_region"] = region
            described_instances[ec2_instance_id] = described_instance

    # Add any missing image IDs to the instance_events from the describes.
    for instance_event in instance_events:
        ec2_instance_id = instance_event.ec2_instance_id
        if instance_event.ec2_ami_id is None and ec2_instance_id in described_instances:
            described_instance = described_instances[ec2_instance_id]
            instance_event.ec2_ami_id = described_instance["ImageId"]

    # We really *should* have what we need, but just in case...
    for ec2_instance_id in all_ec2_instance_ids:
        if (
            ec2_instance_id not in defined_ec2_instance_ids
            and ec2_instance_id not in described_instances
        ):
            logger.info(
                _(
                    "EC2 Instance %(ec2_instance_id)s could not be loaded "
                    "from database or AWS. It may have been terminated before "
                    "we processed it."
                ),
                {"ec2_instance_id": ec2_instance_id},
            )

    return described_instances


def _load_missing_ami_data(instance_events, ami_tag_events):
    """
    Load additional data so we can create the AMIs for the given events.

    Args:
        instance_events (list[CloudTrailInstanceEvent]): found instance events
        ami_tag_events (list[CloudTrailImageTagEvent]): found AMI tag events

    Returns:
         dict: Dict of dicts from AWS describing AMIs that are referenced
            by the input arguments but are not present in our database, with
            the outer key being each AMI's ID.

    """
    seen_ami_ids = set(
        [
            event.ec2_ami_id
            for event in instance_events + ami_tag_events
            if event.ec2_ami_id is not None
        ]
    )
    known_images = AwsMachineImage.objects.filter(ec2_ami_id__in=seen_ami_ids)
    known_ami_ids = set([image.ec2_ami_id for image in known_images])
    new_ami_ids = seen_ami_ids.difference(known_ami_ids)

    new_amis_keyed = set(
        [
            (event.aws_account_id, event.region, event.ec2_ami_id)
            for event in instance_events + ami_tag_events
            if event.ec2_ami_id in new_ami_ids
        ]
    )

    described_amis = dict()

    # Look up only the new AMIs that belong to each account+region group.
    for (aws_account_id, region), amis_keyed in itertools.groupby(
        new_amis_keyed, key=lambda a: (a[0], a[1])
    ):
        amis_keyed = list(amis_keyed)
        awsaccount = AwsCloudAccount.objects.get(aws_account_id=aws_account_id)
        session = aws.get_session(awsaccount.account_arn, region)

        ami_ids = [k[2] for k in amis_keyed]

        # Get all relevant images in one API call for this account+region.
        new_described_amis = aws.describe_images(session, ami_ids, region)
        for described_ami in new_described_amis:
            ami_id = described_ami["ImageId"]
            logger.info(
                _(
                    "Loading data for AMI %(ami_id)s for "
                    "ARN %(account_arn)s in region %(region)s"
                ),
                {
                    "ami_id": ami_id,
                    "account_arn": awsaccount.account_arn,
                    "region": region,
                },
            )
            described_ami["found_in_region"] = region
            described_ami["found_by_account_arn"] = awsaccount.account_arn
            described_amis[ami_id] = described_ami

    for aws_account_id, region, ec2_ami_id in new_amis_keyed:
        if ec2_ami_id not in described_amis:
            logger.info(
                _(
                    "AMI %(ec2_ami_id)s could not be found in region "
                    "%(region)s for AWS account %(aws_account_id)s."
                ),
                {
                    "ec2_ami_id": ec2_ami_id,
                    "region": region,
                    "aws_account_id": aws_account_id,
                },
            )

    return described_amis


@transaction.atomic  # noqa: C901
def _save_cloudtrail_activity(
    instance_events, ami_tag_events, described_instances, described_images
):
    """
    Save new images and instances events found via CloudTrail to the DB.

    The order of operations here generally looks like:

        1. Save new images.
        2. Save tag changes for images.
        3. Save new instances.
        4. Save events for instances.

    Note:
        Nothing should be reaching out to AWS APIs in this function! We should
        have all the necessary information already, and this function saves all
        of it atomically in a single transaction.

    Args:
        instance_events (list[CloudTrailInstanceEvent]): found instance events
        ami_tag_events (list[CloudTrailImageTagEvent]): found ami tag events
        described_instances (dict): described new-to-us AWS instances keyed by
            EC2 instance ID
        described_images (dict): described new-to-us AMIs keyed by AMI ID

    Returns:
        dict: Only the new images that were created in the process.

    """
    # Log some basic information about what we're saving.
    log_prefix = "analyzer"
    all_ec2_instance_ids = set(
        [
            instance_event.ec2_instance_id
            for instance_event in instance_events
            if instance_event.ec2_instance_id is not None
        ]
    )
    logger.info(
        _("%(prefix)s: EC2 Instance IDs found: %(all_ec2_instance_ids)s"),
        {"prefix": log_prefix, "all_ec2_instance_ids": all_ec2_instance_ids},
    )

    all_ami_ids = set(
        [
            instance_event.ec2_ami_id
            for instance_event in instance_events
            if instance_event.ec2_ami_id is not None
        ]
        + [
            ami_tag_event.ec2_ami_id
            for ami_tag_event in ami_tag_events
            if ami_tag_event.ec2_ami_id is not None
        ]
        + [ec2_ami_id for ec2_ami_id in described_images.keys()]
    )
    logger.info(
        _("%(prefix)s: EC2 AMI IDs found: %(all_ami_ids)s"),
        {"prefix": log_prefix, "all_ami_ids": all_ami_ids},
    )

    # Which images have the Windows platform?
    windows_ami_ids = {
        ami_id
        for ami_id, described_ami in described_images.items()
        if is_windows(described_ami)
    }
    logger.info(
        _("%(prefix)s: Windows AMI IDs found: %(windows_ami_ids)s"),
        {"prefix": log_prefix, "windows_ami_ids": windows_ami_ids},
    )

    # Which images need tag state changes?
    ocp_tagged_ami_ids, ocp_untagged_ami_ids = _extract_ami_ids_by_tag_change(
        ami_tag_events, OPENSHIFT_TAG
    )
    logger.info(
        _("%(prefix)s: AMIs found tagged for OCP: %(ocp_tagged_ami_ids)s"),
        {"prefix": log_prefix, "ocp_tagged_ami_ids": ocp_tagged_ami_ids},
    )
    logger.info(
        _("%(prefix)s: AMIs found untagged for OCP: %(ocp_untagged_ami_ids)s"),
        {"prefix": log_prefix, "ocp_untagged_ami_ids": ocp_untagged_ami_ids},
    )

    rhel_tagged_ami_ids, rhel_untagged_ami_ids = _extract_ami_ids_by_tag_change(
        ami_tag_events, RHEL_TAG
    )
    logger.info(
        _("%(prefix)s: AMIs found tagged for RHEL: %(rhel_tagged_ami_ids)s"),
        {"prefix": log_prefix, "rhel_tagged_ami_ids": rhel_tagged_ami_ids},
    )
    logger.info(
        _("%(prefix)s: AMIs found untagged for RHEL: %(rhel_untagged_ami_ids)s"),
        {"prefix": log_prefix, "rhel_untagged_ami_ids": rhel_untagged_ami_ids},
    )

    # Create only the new images.
    new_images = {}
    for ami_id, described_image in described_images.items():
        owner_id = Decimal(described_image["OwnerId"])
        name = described_image["Name"]
        windows = ami_id in windows_ami_ids
        rhel_detected_by_tag = ami_id in rhel_tagged_ami_ids
        openshift_detected = ami_id in ocp_tagged_ami_ids
        region = described_image["found_in_region"]

        logger.info(
            _("%(prefix)s: Saving new AMI ID %(ami_id)s in region %(region)s"),
            {"prefix": log_prefix, "ami_id": ami_id, "region": region},
        )
        awsimage, new = save_new_aws_machine_image(
            ami_id,
            name,
            owner_id,
            rhel_detected_by_tag,
            openshift_detected,
            windows,
            region,
        )

        image = awsimage.machine_image.get()
        if new and image.status is not image.INSPECTED:
            new_images[ami_id] = awsimage

    # Create "unavailable" images for AMIs we saw referenced but that we either
    # don't have in our models or could not describe from AWS.
    seen_ami_ids = set(
        [
            described_instance["ImageId"]
            for described_instance in described_instances.values()
            if described_instance.get("ImageId") is not None
        ]
        + [
            ami_tag_event.ec2_ami_id
            for ami_tag_event in ami_tag_events
            if ami_tag_event.ec2_ami_id is not None
        ]
        + [
            instance_event.ec2_ami_id
            for instance_event in instance_events
            if instance_event.ec2_ami_id is not None
        ]
    )
    described_ami_ids = set(described_images.keys())
    known_ami_ids = set(
        image.ec2_ami_id
        for image in AwsMachineImage.objects.filter(
            ec2_ami_id__in=list(seen_ami_ids - described_ami_ids)
        )
    )
    unavailable_ami_ids = seen_ami_ids - described_ami_ids - known_ami_ids
    for ami_id in unavailable_ami_ids:
        logger.info(
            _("Missing image data for %s; creating UNAVAILABLE stub image."), ami_id
        )
        with transaction.atomic():
            awsmachineimage = AwsMachineImage.objects.create(ec2_ami_id=ami_id)
            MachineImage.objects.create(
                status=MachineImage.UNAVAILABLE, content_object=awsmachineimage
            )
            awsmachineimage.machine_image.get()

    # Update images with openshift tag changes.
    if ocp_tagged_ami_ids:
        MachineImage.objects.filter(
            aws_machine_image__ec2_ami_id__in=ocp_tagged_ami_ids
        ).update(openshift_detected=True)
    if ocp_untagged_ami_ids:
        MachineImage.objects.filter(
            aws_machine_image__ec2_ami_id__in=ocp_untagged_ami_ids
        ).update(openshift_detected=False)

    # Update images with RHEL tag changes.
    if rhel_tagged_ami_ids:
        MachineImage.objects.filter(
            aws_machine_image__ec2_ami_id__in=rhel_tagged_ami_ids
        ).update(rhel_detected_by_tag=True)
    if rhel_untagged_ami_ids:
        MachineImage.objects.filter(
            aws_machine_image__ec2_ami_id__in=rhel_untagged_ami_ids
        ).update(rhel_detected_by_tag=False)

    # Save instances and their events.
    for ((ec2_instance_id, region, aws_account_id), events) in itertools.groupby(
        instance_events, key=lambda e: (e.ec2_instance_id, e.region, e.aws_account_id),
    ):
        events = list(events)

        if ec2_instance_id in described_instances:
            instance_data = described_instances[ec2_instance_id]
        else:
            instance_data = {
                "InstanceId": ec2_instance_id,
                "ImageId": events[0].ec2_ami_id,
                "SubnetId": events[0].subnet_id,
            }
        logger.info(
            _(
                "%(prefix)s: Saving new EC2 instance ID %(ec2_instance_id)s "
                "for AWS account ID %(aws_account_id)s in region %(region)s"
            ),
            {
                "prefix": log_prefix,
                "ec2_instance_id": ec2_instance_id,
                "aws_account_id": aws_account_id,
                "region": region,
            },
        )

        awsaccount = AwsCloudAccount.objects.get(aws_account_id=aws_account_id)
        account = awsaccount.cloud_account.get()
        instance = save_instance(account, instance_data, region)

        # Build a list of event data
        events_info = _build_events_info_for_saving(account, instance, events)
        save_instance_events(instance, instance_data, events_info)

    return new_images


def _extract_ami_ids_by_tag_change(ami_tag_events, tag):
    """
    Extract the AMI IDs from a list of events that have tag-related changes.

    If the same AMI has changed multiple times for the same tag, we only care about
    the most recent change. For example, if an AMI creates and then deletes the tag, the
    net effect is that the tag was deleted, and the AMI ID should only be included in
    the set of "untagged AMI IDs".

    Args:
        ami_tag_events list(CloudTrailImageTagEvent): list of CloudTrailImageTagEvents
        tag (str): the tag for filtering

    Returns:
        tuple (set, set). First value is the set of AMI IDs where the tag was created,
        and the second is the set of AMI IDs where the tag was deleted.

    """
    tagged_ami_ids = set()
    untagged_ami_ids = set()
    for ec2_ami_id, events_info in itertools.groupby(
        ami_tag_events, key=lambda e: e.ec2_ami_id
    ):
        ordered_events = sorted(
            itertools.filterfalse(lambda e: e.tag != tag, events_info),
            key=lambda e: e.occurred_at,
        )
        for event in ordered_events[::-1]:
            # stop looking for tag changes after we find one.
            if ec2_ami_id not in tagged_ami_ids and ec2_ami_id not in untagged_ami_ids:
                if event.exists:
                    tagged_ami_ids.add(ec2_ami_id)
                else:
                    untagged_ami_ids.add(ec2_ami_id)
    return tagged_ami_ids, untagged_ami_ids


def _instance_event_is_complete(instance_event):
    """Check if the instance_event is populated enough for its instance."""
    return (
        instance_event.instance_type is not None
        and instance_event.ec2_ami_id is not None
    )


@retriable_shared_task
def repopulate_ec2_instance_mapping():
    """
    Use the Boto3 pricing client to update the EC2 instancetype lookup table.

    Returns:
        None: Run as an asynchronous Celery task.

    """
    definitions = _fetch_ec2_instance_type_definitions()
    with transaction.atomic():
        try:
            _save_ec2_instance_type_definitions(definitions)
        except Exception as e:
            logger.exception(
                _("Failed to save EC2 instance definitions; rolling back.")
            )
            raise e
    logger.info(_("Finished saving AWS EC2 instance type definitions."))


@task
def verify_account_permissions(account_arn):
    """
    Periodic task that verifies the account arn is still valid.

    Args:
        account_arn: The ARN for the account to check

    Returns:
        bool: True if the ARN is still valid.

    """
    valid = False
    try:
        valid = verify_permissions(account_arn)
    except ValidationError as e:
        logger.info(
            _("ARN %s failed validation. Disabling the cloud account."), account_arn,
        )
        # Disable the cloud account.
        aws_cloud_account = AwsCloudAccount.objects.get(account_arn=account_arn)
        aws_cloud_account.cloud_account.get().disable(message=str(e.detail))

    logger.debug(
        _("ARN %(account_arn)s is valid: %(valid)s."),
        {"account_arn": account_arn, "valid": valid,},
    )

    return valid


def _fetch_ec2_instance_type_definitions():
    """
    Fetch EC2 instance type definitions from AWS Pricing API.

    Returns:
        dict: definitions dict of dicts where the outer key is the instance
        type name and the inner dict has keys memory and vcpu. For example:
        {'r5.large': {'memory': 24.0, 'vcpu': 1}}

    """
    client = boto3.client("pricing")
    paginator = client.get_paginator("get_products")
    page_iterator = paginator.paginate(
        ServiceCode="AmazonEC2",
        Filters=[
            {
                "Type": "TERM_MATCH",
                "Field": "productFamily",
                "Value": "Compute Instance",
            },
        ],
    )
    logger.info(_("Getting AWS EC2 instance type information."))
    instances = {}
    for page in page_iterator:
        for instance in page["PriceList"]:
            try:
                instance_attr = json.loads(instance)["product"]["attributes"]

                # memory comes in formatted like: 1,952.00 GiB
                memory = float(instance_attr.get("memory", 0)[:-4].replace(",", ""))
                vcpu = int(instance_attr.get("vcpu", 0))

                instances[instance_attr["instanceType"]] = {
                    "memory": memory,
                    "vcpu": vcpu,
                }
            except ValueError:
                logger.error(
                    _(
                        "Could not fetch EC2 definition for instance-type "
                        "%(instance_type)s, memory %(memory)s, vcpu %(vcpu)s."
                    ),
                    {
                        "instance_type": instance_attr["instanceType"],
                        "memory": instance_attr.get("memory", 0),
                        "vcpu": instance_attr.get("vcpu", 0),
                    },
                )
    return instances


def _save_ec2_instance_type_definitions(definitions):
    """
    Save AWS EC2 instance type definitions to our database.

    Note:
        If an instance type name already exists in the DB, do NOT overwrite it.

    Args:
        definitions (dict): dict of dicts where the outer key is the instance
            type name and the inner dict has keys memory and vcpu. For example:
            {'r5.large': {'memory': 24.0, 'vcpu': 1}}

    Returns:
        None

    """
    for name, attributes in definitions.items():
        try:
            obj, created = AwsEC2InstanceDefinition.objects.get_or_create(
                instance_type=name,
                defaults={"memory": attributes["memory"], "vcpu": attributes["vcpu"]},
            )
            if created:
                logger.info(_("Saving new instance type %s"), obj.instance_type)
            else:
                logger.info(_("Instance type %s already exists."), obj.instance_type)
        except IntegrityError as e:
            logger.exception(
                _(
                    "Failed to get_or_create an AwsEC2InstanceDefinition("
                    'name="%(name)s", memory=%(memory)s, vcpu=%(vcpu)s'
                    "); this should never happen."
                ),
                {
                    "name": name,
                    "memory": attributes["memory"],
                    "vcpu": attributes["vcpu"],
                },
            )
            raise e


def _build_events_info_for_saving(account, instance, events):
    """
    Build a list of enough information to save the relevant events.

    Of particular note here is the "if" that filters away events that seem to
    have occurred before their account was created. This can happen in some
    edge-case circumstances when the user is deleting and recreating their
    account in cloudigrade while powering off and on events in AWS. The AWS
    CloudTrail from *before* deleting the account may continue to accumulate
    events for some time since it is delayed, and when the account is recreated
    in cloudigrade, those old events may arrive, but we *should not* know about
    them. If we were to keep those events, bad things could happen because we
    may not have enough information about them (instance type, relevant image)
    to process them for reporting.

    Args:
        account (CloudAccount): the account that owns the instance
        instance (Instance): the instance that generated the events
        events (list[CloudTrailInstanceEvent]): the incoming events

    Returns:
        list[dict]: enough information to save a list of events

    """
    events_info = [
        {
            "subnet": getattr(instance, "subnet_id", None),
            "ec2_ami_id": getattr(instance, "image_id", None),
            "instance_type": instance_event.instance_type
            if instance_event.instance_type is not None
            else getattr(instance, "instance_type", None),
            "event_type": instance_event.event_type,
            "occurred_at": instance_event.occurred_at,
        }
        for instance_event in events
        if parse(instance_event.occurred_at) >= account.created_at
    ]
    return events_info
