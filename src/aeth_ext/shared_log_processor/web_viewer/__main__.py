# First party imports
from aeth_ext.shared_log_processor.web_viewer import LogWebViewApp

SKIP_ENTRYPOINT_MARKER = True

if __name__ == "__main__":
  LogWebViewApp().run()
