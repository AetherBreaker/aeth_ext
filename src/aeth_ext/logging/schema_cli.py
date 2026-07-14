"""CLI for generating the JSON schema of the `dict_config` logging configuration."""

# Standard library imports
from pathlib import Path  # noqa: TC003
from typing import Annotated

# Third party imports
import orjson
import typer

# First party imports
from aeth_ext.logging.dict_config import LoggingConfigModel


def cli(
  indent: Annotated[bool, typer.Option(help="Whether to pretty-print (2-space indent) the printed JSON schema.")] = True,
  output: Annotated[Path | None, typer.Option(help="If given, write the schema to this path instead of stdout.")] = None,
) -> None:
  """Generate the JSON schema for the `LoggingConfigModel` logging configuration."""
  options = orjson.OPT_INDENT_2 if indent else 0
  schema = orjson.dumps(LoggingConfigModel.model_json_schema(), option=options).decode("utf-8")
  if output is not None:
    output.write_text(schema, encoding="utf-8")
  else:
    print(schema)


if __name__ == "__main__":
  typer.run(cli)
