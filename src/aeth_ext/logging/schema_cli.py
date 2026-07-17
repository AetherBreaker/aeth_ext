"""CLI for generating the JSON schema of the `dict_config` logging configuration."""

# Standard library imports
from pathlib import Path
from typing import Annotated

# Third party imports
import orjson
import typer

# First party imports
from aeth_ext.logging.config.models import LoggingConfigModel

schema_output_loc = Path(__file__).parent / "config" / "defaults" / "logging_config_schema.json"


app = typer.Typer()


@app.command()
def cli(
  indent: Annotated[bool, typer.Option(help="Whether to pretty-print (2-space indent) the printed JSON schema.")] = True,
  output: Annotated[Path, typer.Option(help="If given, write the schema to this path instead of stdout.")] = (schema_output_loc),
) -> None:
  """Generate the JSON schema for the `LoggingConfigModel` logging configuration."""
  options = orjson.OPT_INDENT_2 if indent else 0
  schema = orjson.dumps(LoggingConfigModel.model_json_schema(), option=options).decode("utf-8")
  output.write_text(schema, encoding="utf-8")
  print(f"JSON schema written to {output}")


if __name__ == "__main__":
  app()
