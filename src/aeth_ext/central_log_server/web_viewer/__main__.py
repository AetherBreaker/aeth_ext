# First party imports
from aeth_ext.central_log_server.web_viewer import LogWebViewApp

SKIP_ENTRYPOINT_MARKER = True

if __name__ == "__main__":
  LogWebViewApp().run()
