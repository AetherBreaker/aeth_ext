# Standard library imports
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, override

# Third party imports
from rich.text import Text
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.widgets import Button, Checkbox, DirectoryTree, Footer, Input, Label, RichLog, Static

# First party imports
from aeth_ext.shared_log_processor.web_viewer.screens.log_picker import ConfirmDeleteModal, FileChosen, LogFileTree

if TYPE_CHECKING:
  # Third party imports
  from textual.app import ComposeResult

  # First party imports
  from aeth_ext.command_client.client import CommandClient
  from aeth_ext.command_server.protocol import CommandMeta


def _prop_type(spec: dict[str, Any]) -> str:
  """Return the primitive JSON-schema type for a property, resolving ``anyOf``."""
  declared = spec.get("type")
  if isinstance(declared, str):
    return declared
  for sub in spec.get("anyOf", []):
    sub_type = sub.get("type")
    if sub_type and sub_type != "null":
      return sub_type
  return "string"


class CommandParamModal(ModalScreen[dict[str, Any] | None]):
  """Dialog that collects parameters for a command from its JSON-schema."""

  def __init__(self, meta: CommandMeta) -> None:
    super().__init__()
    self._meta = meta
    schema = meta.params_schema or {}
    self._properties: dict[str, dict[str, Any]] = schema.get("properties", {})
    self._required: set[str] = set(schema.get("required", []))

  @override
  def compose(self) -> ComposeResult:
    with Vertical(id="param-dialog"):
      yield Static(f"{self._meta.name} parameters", classes="param-title")
      with VerticalScroll(id="param-fields"):
        for name, spec in self._properties.items():
          yield Label(self._field_label(name), classes="param-label")
          yield self._make_widget(name, spec)
      yield Static("", id="param-error")
      with Horizontal(id="param-buttons"):
        yield Button("Run", variant="primary", id="param-submit")
        yield Button("Cancel", id="param-cancel")

  def _field_label(self, name: str) -> str:
    return f"{name} *" if name in self._required else name

  def _make_widget(self, name: str, spec: dict[str, Any]):
    prop_type = _prop_type(spec)
    default = spec.get("default")
    widget_id = f"field-{name}"
    if prop_type == "boolean":
      return Checkbox(value=bool(default) if default is not None else False, id=widget_id)
    if prop_type in ("integer", "number"):
      return Input(value="" if default is None else str(default), type=prop_type, id=widget_id)
    return Input(value="" if default is None else str(default), id=widget_id)

  def _collect(self) -> dict[str, Any]:
    params: dict[str, Any] = {}
    for name, spec in self._properties.items():
      prop_type = _prop_type(spec)
      widget = self.query_one(f"#field-{name}")
      if isinstance(widget, Checkbox):
        params[name] = widget.value
        continue
      assert isinstance(widget, Input)
      raw = widget.value.strip()
      if raw == "":
        if name in self._required:
          raise ValueError(f"{name} is required")
        continue
      if prop_type == "integer":
        params[name] = int(raw)
      elif prop_type == "number":
        params[name] = float(raw)
      else:
        params[name] = raw
    return params

  def on_button_pressed(self, event: Button.Pressed) -> None:
    if event.button.id == "param-cancel":
      self.dismiss(None)
      return
    try:
      params = self._collect()
    except ValueError as exc:
      self.query_one("#param-error", Static).update(str(exc))
      return
    self.dismiss(params)


class CommandButton(Button):
  """A button that invokes a single command, showing its description as a tooltip."""

  def __init__(self, meta: CommandMeta) -> None:
    super().__init__(meta.name, classes="command-button")
    self.meta = meta
    self.tooltip = meta.description or None


class CommandsPanel(Vertical):
  """Left-hand panel listing a program's commands with a small output log."""

  def __init__(self, program_name: str, command_client: CommandClient, **kwargs: Any) -> None:
    super().__init__(**kwargs)
    self._program_name = program_name
    self._command_client = command_client

  @override
  def compose(self) -> ComposeResult:
    yield Static("Commands", classes="panel-heading")
    yield VerticalScroll(id="command-buttons")
    yield RichLog(id="cmd-output", max_lines=200, wrap=True, markup=False)

  async def on_mount(self) -> None:
    container = self.query_one("#command-buttons", VerticalScroll)
    commands = self._command_client.commands_for(self._program_name)
    if not commands:
      await container.mount(Static("No commands available.", classes="empty-message"))
      return
    await container.mount(*(CommandButton(meta) for meta in commands))

  async def on_button_pressed(self, event: Button.Pressed) -> None:
    button = event.button
    if not isinstance(button, CommandButton):
      return
    meta = button.meta
    if self._properties_of(meta):
      params = await self.app.push_screen_wait(CommandParamModal(meta))
      if params is None:
        return
    else:
      params = {}
    self.run_worker(self._invoke(meta, params), exclusive=False)

  @staticmethod
  def _properties_of(meta: CommandMeta) -> dict[str, Any]:
    return (meta.params_schema or {}).get("properties", {})

  async def _invoke(self, meta: CommandMeta, params: dict[str, Any]) -> None:
    output = self.query_one("#cmd-output", RichLog)
    invoked = Text()
    invoked.append(meta.name, style="bold")
    invoked.append(" invoked", style="dim")
    output.write(invoked)
    try:
      result = await self._command_client.invoke(self._program_name, meta.name, **params)
    except Exception as exc:  # noqa: BLE001 - surface any failure to the panel
      line = Text()
      line.append("error: ", style="bold red")
      line.append(str(exc))
      output.write(line)
      return
    line = Text()
    if meta.returns_value:
      line.append("result: ", style="bold green")
      line.append(repr(result))
    else:
      line.append("ok", style="bold green")
    output.write(line)


class ProgramScreen(Screen[None]):
  """Per-program screen: a commands panel beside a filtered log-file picker."""

  CSS_PATH = Path(__file__).with_suffix(".tcss")

  BINDINGS: ClassVar[list[tuple[str, str, str]]] = [
    ("escape", "back", "Back"),
    ("r", "refresh", "Refresh"),
    ("f5", "refresh", ""),
    ("d", "delete_file", "Delete File"),
  ]

  def __init__(self, program_name: str, log_root: Path, command_client: CommandClient) -> None:
    super().__init__()
    self._program_name = program_name
    self._log_root = log_root
    self._command_client = command_client
    self._program_dir = log_root / program_name

  @override
  def compose(self) -> ComposeResult:
    yield Static(f"Program: {self._program_name}", id="program-title")
    with Horizontal(id="program-body"):
      yield CommandsPanel(self._program_name, self._command_client, id="commands-panel")
      with Vertical(id="log-panel"):
        yield Static("Log files", classes="panel-heading")
        if self._program_dir.is_dir():
          yield LogFileTree(self._program_dir, id="log-tree")
        else:
          yield Static("No log files for this program.", classes="empty-message")
    yield Footer()

  def on_directory_tree_file_selected(self, event: DirectoryTree.FileSelected) -> None:
    selected = event.path
    if selected.suffix.lower() != ".txt":
      self.notify("Only .txt log files can be streamed", severity="warning")
      return
    self.post_message(FileChosen(selected))

  def action_back(self) -> None:
    self.app.pop_screen()

  async def action_refresh(self) -> None:
    if not self._program_dir.is_dir():
      return
    tree = self.query_one(LogFileTree)
    await tree.load_metadata()
    await tree.reload()

  async def action_delete_file(self) -> None:
    if not self._program_dir.is_dir():
      return
    tree = self.query_one(LogFileTree)
    node = tree.cursor_node
    if node is None or node.data is None or not node.data.path.is_file():
      self.notify("Highlight a file first", severity="warning")
      return

    path = node.data.path
    confirmed: bool = await self.app.push_screen_wait(ConfirmDeleteModal(path))
    if not confirmed:
      return

    try:
      path.unlink()
      self.notify(f"Deleted {path.name}")
      await tree.reload()
    except OSError as exc:
      self.notify(f"Delete failed: {exc}", severity="error")
