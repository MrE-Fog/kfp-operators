#!/usr/bin/env python3
# Copyright 2023 Canonical Ltd.
# See LICENSE file for licensing details.

"""Charm the Kubeflow Pipelines API.

https://github.com/canonical/kfp-operators/
"""

import json
import logging
from pathlib import Path

from charmed_kubeflow_chisme.exceptions import ErrorWithStatus, GenericCharmRuntimeError
from charmed_kubeflow_chisme.kubernetes import KubernetesResourceHandler
from charmed_kubeflow_chisme.lightkube.batch import delete_many
from charmed_kubeflow_chisme.pebble import update_layer
from charms.data_platform_libs.v0.data_interfaces import DatabaseRequires
from charms.observability_libs.v1.kubernetes_service_patch import KubernetesServicePatch
from charms.prometheus_k8s.v0.prometheus_scrape import MetricsEndpointProvider
from jsonschema import ValidationError
from lightkube import ApiError
from lightkube.generic_resource import load_in_cluster_generic_resources
from lightkube.models.core_v1 import ServicePort
from ops.charm import CharmBase
from ops.main import main
from ops.model import (
    ActiveStatus,
    BlockedStatus,
    Container,
    MaintenanceStatus,
    ModelError,
    WaitingStatus,
)
from ops.pebble import CheckStatus, Layer
from serialized_data_interface import (
    NoCompatibleVersions,
    NoVersionsListed,
    SerializedDataInterface,
    get_interfaces,
)
from serialized_data_interface.errors import RelationDataError

CONFIG_DIR = Path("/config")
SAMPLE_CONFIG = CONFIG_DIR / "sample_config.json"
METRICS_PATH = "/metrics"
PROBE_PATH = "/apis/v1beta1/healthz"

K8S_RESOURCE_FILES = [
    "src/templates/auth_manifests.yaml.j2",
    "src/templates/ml-pipeline-service.yaml.j2",
]
MYSQL_WARNING = "Relation mysql is deprecated."
UNBLOCK_MESSAGE = "Remove deprecated mysql relation to unblock."
KFP_API_SERVICE_NAME = "apiserver"


class KfpApiOperator(CharmBase):
    """Charm the Kubeflow Pipelines API."""

    def __init__(self, *args):
        super().__init__(*args)

        # retrieve configuration and base settings
        self.logger = logging.getLogger(__name__)
        self._namespace = self.model.name
        self._lightkube_field_manager = "lightkube"
        self._name = self.model.app.name
        self._grcp_port = self.model.config["grpc-port"]
        self._http_port = self.model.config["http-port"]
        self._exec_command = (
            # TODO: Remove 'sleep' as soon as a fix for
            # https://github.com/canonical/pebble/issues/240 is provided
            "sleep 1.1 && "
            "/bin/apiserver "
            f"--config={CONFIG_DIR} "
            f"--sampleconfig={SAMPLE_CONFIG} "
            "-logtostderr=true "
        )
        self._container_name = "ml-pipeline-api-server"
        self._database_name = "mlpipeline"
        self._container = self.unit.get_container(self._container_name)

        # setup context to be used for updating K8S resources
        self._context = {
            "app_name": self._name,
            "namespace": self._namespace,
            "service": self._name,
            "grpc_port": self._grcp_port,
            "http_port": self._http_port,
        }
        self._k8s_resource_handler = None

        grpc_port = ServicePort(int(self._grcp_port), name="grpc-port")
        http_port = ServicePort(int(self._http_port), name="http-port")
        self.service_patcher = KubernetesServicePatch(
            self,
            [grpc_port, http_port],
        )

        # setup events to be handled by main event handler
        self.framework.observe(self.on.leader_elected, self._on_event)
        self.framework.observe(self.on.config_changed, self._on_event)
        self.framework.observe(self.on.ml_pipeline_api_server_pebble_ready, self._on_event)
        change_events = [
            self.on["object-storage"].relation_changed,
            self.on["kfp-viz"].relation_changed,
            self.on["kfp-api"].relation_changed,
        ]
        for event in change_events:
            self.framework.observe(event, self._on_event)

        # setup events to be handled by specific event handlers
        self.framework.observe(self.on.install, self._on_install)
        self.framework.observe(self.on.upgrade_charm, self._on_upgrade)
        self.framework.observe(self.on.remove, self._on_remove)
        self.framework.observe(self.on.update_status, self._on_update_status)
        self.framework.observe(self.on["mysql"].relation_joined, self._on_mysql_relation)
        self.framework.observe(self.on["mysql"].relation_changed, self._on_mysql_relation)
        self.framework.observe(self.on["mysql"].relation_departed, self._on_mysql_relation_remove)
        self.framework.observe(self.on["mysql"].relation_broken, self._on_mysql_relation_remove)
        self.framework.observe(
            self.on["relational-db"].relation_joined, self._on_relational_db_relation
        )
        self.framework.observe(
            self.on["relational-db"].relation_changed, self._on_relational_db_relation
        )
        self.framework.observe(
            self.on["relational-db"].relation_departed, self._on_relational_db_relation_remove
        )
        self.framework.observe(
            self.on["relational-db"].relation_broken, self._on_relational_db_relation_remove
        )

        # setup relational database interface and observers
        self.database = DatabaseRequires(
            self, relation_name="relational-db", database_name=self._database_name
        )
        self.framework.observe(self.database.on.database_created, self._on_relational_db_relation)
        self.framework.observe(self.database.on.endpoints_changed, self._on_relational_db_relation)

        self.prometheus_provider = MetricsEndpointProvider(
            charm=self,
            relation_name="metrics-endpoint",
            jobs=[
                {
                    "metrics_path": METRICS_PATH,
                    "static_configs": [{"targets": ["*:{}".format(self.config["http-port"])]}],
                }
            ],
        )

    @property
    def container(self):
        """Return container."""
        return self._container

    @property
    def k8s_resource_handler(self):
        """Update K8S with K8S resources."""
        if not self._k8s_resource_handler:
            self._k8s_resource_handler = KubernetesResourceHandler(
                field_manager=self._lightkube_field_manager,
                template_files=K8S_RESOURCE_FILES,
                context=self._context,
                logger=self.logger,
            )
        load_in_cluster_generic_resources(self._k8s_resource_handler.lightkube_client)
        return self._k8s_resource_handler

    @k8s_resource_handler.setter
    def k8s_resource_handler(self, handler: KubernetesResourceHandler):
        self._k8s_resource_handler = handler

    @property
    def service_environment(self):
        """Return environment variables based on model configuration."""
        ret_env_vars = {"POD_NAMESPACE": self.model.name}

        return ret_env_vars

    @property
    def _kfp_api_layer(self) -> Layer:
        """Create and return Pebble framework layer."""
        # The service name should be the same as the one
        # defined in the Rockcraft project: apiserver
        layer_config = {
            "summary": "kfp-api layer",
            "description": "Pebble config layer for kfp-api",
            "services": {
                KFP_API_SERVICE_NAME: {
                    "override": "replace",
                    "summary": "ML Pipeline API Server",
                    "command": f"bash -c '{self._exec_command}'",
                    "startup": "enabled",
                    "environment": self.service_environment,
                    "on-check-failure": {"kfp-api-up": "restart"},
                }
            },
            "checks": {
                "kfp-api-up": {
                    "override": "replace",
                    "period": "5m",
                    "timeout": "60s",
                    "threshold": 3,
                    "http": {"url": f"http://localhost:{self.config['http-port']}{PROBE_PATH}"},
                }
            },
        }

        return Layer(layer_config)

    def _generate_config(self, interfaces):
        """Generate configuration based on supplied data.

        Configuration is generated based on:
        - Supplied interfaces.
        - Database data: from MySQL relation data or from data platform library.
        - Model configuration.
        """

        config = self.model.config
        try:
            db_data = self._get_db_data()
            os = self._get_object_storage(interfaces)
            viz = self._get_viz(interfaces)
        except ErrorWithStatus as error:
            self.logger.error("Failed to generate container configuration.")
            raise error

        # at this point all data is correctly populated and proper config can be generated
        config_json = {
            "DBConfig": {
                "ConMaxLifeTime": "120s",
                "DBName": db_data["db_name"],
                "DriverName": "mysql",
                "GroupConcatMaxLen": "4194304",
                "Host": db_data["db_host"],
                "Password": db_data["db_password"],
                "Port": db_data["db_port"],
                "User": db_data["db_username"],
            },
            "ObjectStoreConfig": {
                "AccessKey": os["access-key"],
                "BucketName": config["object-store-bucket-name"],
                "Host": f"{os['service']}.{os['namespace']}",
                "Multipart": {"Disable": "true"},
                "PipelinePath": "pipelines",
                "Port": str(os["port"]),
                "Region": "",
                "SecretAccessKey": os["secret-key"],
                "Secure": str(os["secure"]).lower(),
            },
            "ARCHIVE_CONFIG_LOG_FILE_NAME": config["log-archive-filename"],
            "ARCHIVE_CONFIG_LOG_PATH_PREFIX": config["log-archive-prefix"],
            "AUTO_UPDATE_PIPELINE_DEFAULT_VERSION": str(
                config["auto-update-default-version"]
            ).lower(),
            "CACHE_IMAGE": config["cache-image"],
            "CACHE_NODE_RESTRICTIONS": "false",
            "CacheEnabled": str(config["cache-enabled"]).lower(),
            "DefaultPipelineRunnerServiceAccount": config["runner-sa"],
            "InitConnectionTimeout": config["init-connection-timeout"],
            "KUBEFLOW_USERID_HEADER": "kubeflow-userid",
            "KUBEFLOW_USERID_PREFIX": "",
            "MULTIUSER": "true",
            "ML_PIPELINE_VISUALIZATIONSERVER_SERVICE_HOST": viz["service-name"],
            "ML_PIPELINE_VISUALIZATIONSERVER_SERVICE_PORT": viz["service-port"],
        }
        return config_json

    def _check_container_connection(self, container: Container) -> None:
        """Check if connection can be made with container.

        Args:
            container: the named container in a unit to check.

        Raises:
            ErrorWithStatus if the connection cannot be made.
        """
        if not container.can_connect():
            raise ErrorWithStatus("Pod startup is not complete", MaintenanceStatus)

    def _check_status(self):
        """Check status of workload and set status accordingly."""
        self._check_leader()
        container = self.unit.get_container(self._container_name)
        if container:
            try:
                # verify if container is alive/up
                check = container.get_check("kfp-api-up")
            except ModelError as error:
                raise GenericCharmRuntimeError(
                    "Failed to run health check on workload container"
                ) from error

            if check.status == CheckStatus.DOWN:
                self.logger.error(
                    f"Container {self._container_name} failed health check. It will be restarted."
                )
                raise ErrorWithStatus("Workload failed health check", MaintenanceStatus)
            self.model.unit.status = ActiveStatus()

    def _upload_files_to_container(self, config_json):
        """Upload required files to container."""
        try:
            self._check_container_connection(self.container)
        except ErrorWithStatus as error:
            self.model.unit.status = error.status
            raise error
        try:
            with open("src/sample_config.json", "r") as sample_config:
                file_content = sample_config.read()
                self.container.push(SAMPLE_CONFIG, file_content, make_dirs=True)
        except ErrorWithStatus as error:
            self.logger.error("Failed to upload sample config to container.")
            raise error
        try:
            file_content = json.dumps(config_json)
            config = CONFIG_DIR / "config.json"
            self.container.push(config, file_content, make_dirs=True)
        except ErrorWithStatus as error:
            self.logger.error("Failed to upload config to container.")
            raise error

    def _send_info(self, interfaces):
        if interfaces["kfp-api"]:
            interfaces["kfp-api"].send_data(
                {
                    "service-name": f"{self.model.app.name}.{self.model.name}",
                    "service-port": self.model.config["http-port"],
                }
            )

    def _get_interfaces(self):
        # Remove this abstraction when SDI adds .status attribute to NoVersionsListed,
        # NoCompatibleVersionsListed:
        # https://github.com/canonical/serialized-data-interface/issues/26
        try:
            interfaces = get_interfaces(self)
        except NoVersionsListed as err:
            raise ErrorWithStatus((err), WaitingStatus)
        except NoCompatibleVersions as err:
            raise ErrorWithStatus(str(err), BlockedStatus)
        except RelationDataError as err:
            raise ErrorWithStatus(str(err), BlockedStatus)
        return interfaces

    def _validate_sdi_interface(self, interfaces: dict, relation_name: str, default_return=None):
        """Validates data received from SerializedDataInterface, returning the data if valid.

        Optionally can return a default_return value when no relation is established

        Raises:
            ErrorWithStatus(..., Blocked) when no relation established (unless default_return set)
            ErrorWithStatus(..., Blocked) if interface is not using SDI
            ErrorWithStatus(..., Blocked) if data in interface fails schema check
            ErrorWithStatus(..., Waiting) if we have a relation established but no data passed

        Params:
            interfaces:

        Returns:
              (dict) interface data
        """
        # If nothing is related to this relation, return a default value or raise an error
        if relation_name not in interfaces or interfaces[relation_name] is None:
            if default_return is not None:
                return default_return
            else:
                raise ErrorWithStatus(
                    f"Please add required relation {relation_name}", BlockedStatus
                )

        relations = interfaces[relation_name]
        if not isinstance(relations, SerializedDataInterface):
            raise ErrorWithStatus(
                f"Unexpected error with {relation_name} relation data - data not as expected",
                BlockedStatus,
            )

        # Get and validate data from the relation
        try:
            # relations is a dict of {(ops.model.Relation, ops.model.Application): data}
            unpacked_relation_data = relations.get_data()
        except ValidationError as val_error:
            # Validation in .get_data() ensures if data is populated, it matches the schema and is
            # not incomplete
            self.logger.error(val_error)
            raise ErrorWithStatus(
                f"Found incomplete/incorrect relation data for {relation_name}. See logs",
                BlockedStatus,
            )

        # Check if we have an established relation with no data exchanged
        if len(unpacked_relation_data) == 0:
            raise ErrorWithStatus(f"Waiting for {relation_name} relation data", WaitingStatus)

        # Unpack data (we care only about the first element)
        data_dict = list(unpacked_relation_data.values())[0]

        # Catch if empty data dict is received (JSONSchema ValidationError above does not raise
        # when this happens)
        # Remove once addressed in:
        # https://github.com/canonical/serialized-data-interface/issues/28
        if len(data_dict) == 0:
            raise ErrorWithStatus(
                f"Found empty relation data for {relation_name}",
                BlockedStatus,
            )

        return data_dict

    def _get_object_storage(self, interfaces):
        """Retrieve object-storage relation data."""
        relation_name = "object-storage"
        return self._validate_sdi_interface(interfaces, relation_name)

    def _get_viz(self, interfaces):
        """Retrieve kfp-viz relation data, return default, if empty."""
        relation_name = "kfp-viz"
        return self._validate_sdi_interface(interfaces, relation_name)

    def _check_leader(self):
        """Check if this unit is a leader."""
        if not self.unit.is_leader():
            self.logger.warning("Not a leader, skipping setup")
            raise ErrorWithStatus("Waiting for leadership", WaitingStatus)

    def _get_db_relation(self, relation_name):
        """Retrieves relation with supplied relation name, if it is established.

        Returns relation, if it is established, and raises error otherwise."""

        try:
            # retrieve relation data
            relation = self.model.get_relation(relation_name)
        except KeyError:
            # relation was not found
            relation = None
        if not relation:
            # relation is not established, raise an error
            raise GenericCharmRuntimeError(
                f"Database relation {relation_name} is not established or empty"
            )

        return relation

    def _get_mysql_data(self) -> dict:
        """Check mysql relation, retrieve and return data, if available."""
        db_data = {}
        relation_data = {}
        relation = self._get_db_relation("mysql")

        # retrieve database data from relation
        try:
            unit = next(iter(relation.units))
            relation_data = relation.data[unit]
            # retrieve database data from relation data
            # this also validates the expected data by means of KeyError exception
            db_data["db_name"] = relation_data["database"]
            db_data["db_password"] = relation_data["root_password"]
            db_data["db_username"] = "root"
            db_data["db_host"] = relation_data["host"]
            db_data["db_port"] = relation_data["port"]
        except (IndexError, StopIteration, KeyError) as err:
            # failed to retrieve database configuration
            if not relation_data:
                raise GenericCharmRuntimeError(
                    "Database relation mysql is not established or empty"
                )
            self.logger.error(f"Missing attribute {err} in mysql relation data")
            # incorrect/incomplete data can be found in mysql relation which can be resolved:
            # use WaitingStatus
            raise ErrorWithStatus(
                "Incorrect/incomplete data found in relation mysql. See logs", WaitingStatus
            )

        return db_data

    def _get_relational_db_data(self) -> dict:
        """Check relational-db relation, retrieve and return data, if available."""
        db_data = {}
        relation_data = {}

        self._get_db_relation("relational-db")

        # retrieve database data from library
        relation_data = self.database.fetch_relation_data()
        # parse data in relation
        # this also validates expected data by means of KeyError exception
        for val in relation_data.values():
            if not val:
                continue
            try:
                db_data["db_name"] = self._database_name
                db_data["db_password"] = val["password"]
                db_data["db_username"] = val["username"]
                host, port = val["endpoints"].split(":")
                db_data["db_host"] = host
                db_data["db_port"] = port
            except KeyError as err:
                self.logger.error(f"Missing attribute {err} in relational-db relation data")
                # incorrect/incomplete data can be found in mysql relation which can be
                # resolved: use WaitingStatus
                raise ErrorWithStatus(
                    "Incorrect/incomplete data found in relation relational-db. See logs",
                    WaitingStatus,
                )
        # report if there was no data populated
        if not db_data:
            self.logger.info("Found empty relation data for relational-db relation.")
            raise ErrorWithStatus("Waiting for relational-db data", WaitingStatus)

        return db_data

    def _get_db_data(self) -> dict:
        """Check for MySQL relations -  mysql or relational-db - and retrieve data.

        Only one database relation can be established at a time.
        """
        db_data = {}
        try:
            db_data = self._get_mysql_data()
        except ErrorWithStatus as err:
            # mysql relation is established, but data could not be retrieved
            raise err
        except GenericCharmRuntimeError:
            # mysql relation is not established, proceed to check for relational-db relation
            try:
                db_data = self._get_relational_db_data()
            except ErrorWithStatus as err:
                # relation-db relation is established, but data could not be retrieved
                raise err
            except GenericCharmRuntimeError:
                # mysql and relational-db relations are not established, raise error
                raise ErrorWithStatus(
                    "Please add required database relation: eg. relational-db", BlockedStatus
                )

        return db_data

    def _check_and_report_k8s_conflict(self, error):
        """Return True if error status code is 409 (conflict), False otherwise."""
        if error.status.code == 409:
            self.logger.warning(f"Encountered a conflict: {error}")
            return True
        return False

    def _apply_k8s_resources(self, force_conflicts: bool = False) -> None:
        """Apply K8S resources.

        Args:
            force_conflicts (bool): *(optional)* Will "force" apply requests causing conflicting
                                    fields to change ownership to the field manager used in this
                                    charm.
                                    NOTE: This will only be used if initial regular apply() fails.
        """
        self.unit.status = MaintenanceStatus("Creating K8S resources")
        try:
            self.k8s_resource_handler.apply()
        except ApiError as error:
            if self._check_and_report_k8s_conflict(error) and force_conflicts:
                # conflict detected when applying K8S resources
                # re-apply K8S resources with forced conflict resolution
                self.unit.status = MaintenanceStatus("Force applying K8S resources")
                self.logger.warning("Apply K8S resources with forced changes against conflicts")
                self.k8s_resource_handler.apply(force=force_conflicts)
            else:
                raise GenericCharmRuntimeError("K8S resources creation failed") from error
        self.model.unit.status = MaintenanceStatus("K8S resources created")

    def _on_install(self, _):
        """Installation only tasks."""
        # deploy K8S resources to speed up deployment
        self._apply_k8s_resources()

    def _on_upgrade(self, _):
        """Perform upgrade steps."""
        # force conflict resolution in K8S resources update
        self._on_event(_, force_conflicts=True)

    def _on_remove(self, _):
        """Remove all resources."""
        self.unit.status = MaintenanceStatus("Removing K8S resources")
        k8s_resources_manifests = self.k8s_resource_handler.render_manifests()
        try:
            delete_many(self.k8s_resource_handler.lightkube_client, k8s_resources_manifests)
        except ApiError as error:
            # do not log/report when resources were not found
            if error.status.code != 404:
                self.logger.error(f"Failed to delete K8S resources, with error: {error}")
                raise error
        self.unit.status = MaintenanceStatus("K8S resources removed")

    def _on_update_status(self, _):
        """Update status actions."""
        try:
            self._on_event(_)
        except ErrorWithStatus:
            return

        if isinstance(self.model.unit.status, WaitingStatus) or isinstance(
            self.model.unit.status, BlockedStatus
        ):
            # do not check status in case of Waiting and Blocked states
            return

        try:
            self._check_status()
        except ErrorWithStatus as err:
            self.model.unit.status = err.status
            self.logger.error(f"Failed update status with error: {err}")
            return

        self.model.unit.status = ActiveStatus()

    def _on_mysql_relation(self, event):
        """Check for existing database relations and process mysql relation if needed."""
        # check for too many mysql relations
        mysql = self.model.relations["mysql"]
        if len(mysql) > 1:
            raise ErrorWithStatus(f"Too many mysql relations. {MYSQL_WARNING}", BlockedStatus)

        # check for relational-db relation
        # relying on KeyError to ensure that relational-db relation is not present
        try:
            relation = self.model.get_relation("relational-db")
            if relation:
                self.logger.warning(
                    "Up-to-date database relation relational-db is already established."
                )
                self.logger.error(f"{MYSQL_WARNING} {UNBLOCK_MESSAGE}")
                self.model.unit.status = BlockedStatus(f"{UNBLOCK_MESSAGE} See logs")
                return
        except KeyError:
            pass
        # relational-db relation was not found, proceed with warnings
        self.logger.warning(MYSQL_WARNING)
        self.model.unit.status = MaintenanceStatus(f"Adding mysql relation. {MYSQL_WARNING}")
        self._on_event(event)

    def _on_mysql_relation_remove(self, event):
        """Process removal of mysql relation."""
        self.model.unit.status = MaintenanceStatus(f"Removing mysql relation. {MYSQL_WARNING}")
        self._on_event(event)

    def _on_relational_db_relation(self, event):
        """Check for existing database relations and process relational-db relation if needed."""
        # relying on KeyError to ensure that mysql relation is not present
        try:
            relation = self.model.get_relation("mysql")
            if relation:
                self.logger.warning(
                    "Failed to create relational-db relation due to existing mysql relation."
                )
                self.logger.error(f"{MYSQL_WARNING} {UNBLOCK_MESSAGE}")
                self.model.unit.status = BlockedStatus(f"{UNBLOCK_MESSAGE} See logs")
                return
        except KeyError:
            pass
        # mysql relation was not found, proceed
        self.model.unit.status = MaintenanceStatus("Adding relational-db relation")
        self._on_event(event)

    def _on_relational_db_relation_remove(self, event):
        """Process removal of relational-db relation."""
        self.model.unit.status = MaintenanceStatus("Removing relational-db relation")
        self._on_event(event)

    def _on_event(self, event, force_conflicts: bool = False) -> None:
        # Set up all relations/fetch required data
        try:
            self._check_leader()
            interfaces = self._get_interfaces()
            config_json = self._generate_config(interfaces)
            self._upload_files_to_container(config_json)
            self._apply_k8s_resources(force_conflicts=force_conflicts)
            update_layer(self._container_name, self._container, self._kfp_api_layer, self.logger)
            self._send_info(interfaces)
        except ErrorWithStatus as err:
            self.model.unit.status = err.status
            self.logger.error(f"Failed to handle {event} with error: {err}")
            return

        self.model.unit.status = ActiveStatus()


if __name__ == "__main__":
    main(KfpApiOperator)
