"""
Packaged default logging-config TOML fragments.

Fragments are atomic pieces of configuration (base sections, file handlers,
console handlers, queue wrapping) selected by runtime flags and assembled via
`aeth_ext.logging.config.loader.assemble_default_config`. The assembled result
is what project override files replace or merge onto.
"""
