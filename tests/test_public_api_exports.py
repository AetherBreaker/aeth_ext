"""Public-API smoke tests, one per module that defines `__all__`.

Guards against a symbol being renamed/removed without updating `__all__` (or
vice versa) -- a silent break for any downstream consumer doing
`from module import *`. Each module is imported for real (not by dotted
string path) so IDE rename-symbol tooling can still track these references
if a module itself is ever moved or renamed.
"""

# Standard library imports
from typing import TYPE_CHECKING

# First party imports
import aeth_ext
import aeth_ext.central_log_server.client.config_provider as config_provider_module
import aeth_ext.central_log_server.client.history as client_history_module
import aeth_ext.central_log_server.client.id_checkpoint as id_checkpoint_module
import aeth_ext.central_log_server.server.dispatch as dispatch_module
import aeth_ext.central_log_server.server.id_registry as id_registry_module
import aeth_ext.errors as errors_init_module
import aeth_ext.errors.err_handling as err_handling_module
import aeth_ext.errors.send_alert_email as send_alert_email_module
import aeth_ext.errors.send_alert_push as send_alert_push_module
import aeth_ext.errors.traceback_image as traceback_image_module
import aeth_ext.ftp as ftp_init_module
import aeth_ext.ftp.credentials as ftp_credentials_module
import aeth_ext.ftp.errors as ftp_errors_module
import aeth_ext.ftp.factory as ftp_factory_module
import aeth_ext.ftp.pool.ftp_adapter as ftp_pool_ftp_adapter_module
import aeth_ext.ftp.pool.sftp_adapter as ftp_pool_sftp_adapter_module
import aeth_ext.ftp.pool.sftp_channel_pool as ftp_pool_sftp_channel_pool_module
import aeth_ext.ftp.session as ftp_session_module
import aeth_ext.ftp.types as ftp_types_module
import aeth_ext.logging.bases as logging_bases_module
import aeth_ext.logging.config.loader as loader_module
import aeth_ext.logging.config.merge as merge_module
import aeth_ext.logging.config.runtime_registry as runtime_registry_module
import aeth_ext.logging.init as logging_init_module
import aeth_ext.logging.setup as logging_setup_module
import aeth_ext.monitoring as monitoring_module
import aeth_ext.monitoring.heartbeat as monitoring_heartbeat_module
import aeth_ext.monitoring.ping as monitoring_ping_module
import aeth_ext.rich.progress as progress_module
import aeth_ext.settings as settings_module
import aeth_ext.static_eval as static_eval_module
import aeth_ext.types as types_init_module
import aeth_ext.types.abc as types_abc_module
import aeth_ext.utils as utils_module

if TYPE_CHECKING:
  # Standard library imports
  from types import ModuleType


def _assert_all_exports_exist(module: ModuleType) -> None:
  missing = [name for name in module.__all__ if not hasattr(module, name)]
  assert missing == [], f"{module.__name__}.__all__ lists names no longer present on the module: {missing}"


class TestPublicApiExports:
  def test_aeth_ext(self) -> None:
    _assert_all_exports_exist(aeth_ext)

  def test_central_log_server_client_config_provider(self) -> None:
    _assert_all_exports_exist(config_provider_module)

  def test_central_log_server_client_history(self) -> None:
    _assert_all_exports_exist(client_history_module)

  def test_central_log_server_client_id_checkpoint(self) -> None:
    _assert_all_exports_exist(id_checkpoint_module)

  def test_central_log_server_server_dispatch(self) -> None:
    _assert_all_exports_exist(dispatch_module)

  def test_central_log_server_server_id_registry(self) -> None:
    _assert_all_exports_exist(id_registry_module)

  def test_errors_init(self) -> None:
    _assert_all_exports_exist(errors_init_module)

  def test_errors_err_handling(self) -> None:
    _assert_all_exports_exist(err_handling_module)

  def test_errors_send_alert_email(self) -> None:
    _assert_all_exports_exist(send_alert_email_module)

  def test_errors_send_alert_push(self) -> None:
    _assert_all_exports_exist(send_alert_push_module)

  def test_errors_traceback_image(self) -> None:
    _assert_all_exports_exist(traceback_image_module)

  def test_ftp_init(self) -> None:
    _assert_all_exports_exist(ftp_init_module)

  def test_ftp_credentials(self) -> None:
    _assert_all_exports_exist(ftp_credentials_module)

  def test_ftp_errors(self) -> None:
    _assert_all_exports_exist(ftp_errors_module)

  def test_ftp_factory(self) -> None:
    _assert_all_exports_exist(ftp_factory_module)

  def test_ftp_pool_ftp_adapter(self) -> None:
    _assert_all_exports_exist(ftp_pool_ftp_adapter_module)

  def test_ftp_pool_sftp_adapter(self) -> None:
    _assert_all_exports_exist(ftp_pool_sftp_adapter_module)

  def test_ftp_pool_sftp_channel_pool(self) -> None:
    _assert_all_exports_exist(ftp_pool_sftp_channel_pool_module)

  def test_ftp_session(self) -> None:
    _assert_all_exports_exist(ftp_session_module)

  def test_ftp_types(self) -> None:
    _assert_all_exports_exist(ftp_types_module)

  def test_logging_bases(self) -> None:
    _assert_all_exports_exist(logging_bases_module)

  def test_logging_config_loader(self) -> None:
    _assert_all_exports_exist(loader_module)

  def test_logging_config_merge(self) -> None:
    _assert_all_exports_exist(merge_module)

  def test_logging_config_runtime_registry(self) -> None:
    _assert_all_exports_exist(runtime_registry_module)

  def test_logging_init(self) -> None:
    _assert_all_exports_exist(logging_init_module)

  def test_logging_setup(self) -> None:
    _assert_all_exports_exist(logging_setup_module)

  def test_monitoring(self) -> None:
    _assert_all_exports_exist(monitoring_module)

  def test_monitoring_heartbeat(self) -> None:
    _assert_all_exports_exist(monitoring_heartbeat_module)

  def test_monitoring_ping(self) -> None:
    _assert_all_exports_exist(monitoring_ping_module)

  def test_rich_progress(self) -> None:
    _assert_all_exports_exist(progress_module)

  def test_settings(self) -> None:
    _assert_all_exports_exist(settings_module)

  def test_static_eval(self) -> None:
    _assert_all_exports_exist(static_eval_module)

  def test_types_init(self) -> None:
    _assert_all_exports_exist(types_init_module)

  def test_types_abc(self) -> None:
    _assert_all_exports_exist(types_abc_module)

  def test_utils(self) -> None:
    _assert_all_exports_exist(utils_module)
