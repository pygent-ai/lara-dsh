# Credentials (lara_shell)

Reads and writes user credential sources. Command-line parsing lives in
`lara_shell.ops.cli.credentials`; this package owns storage only.

## Layering

Credentials are an **environment capability**, not kernel state:

- `lara_shell.config.load_run_config` loads the user credential file into the
  process environment before building a run configuration;
- `lara_shell.credentials.build_credential_environment` injects the
  store-backed lookup (user file, environment, keyring) into the kernel's
  `lara.runtime.model_configuration.CredentialEnvironment` at the composition
  root;
- the kernel defines only the lookup protocol and an environment-only default,
  so `lara` never imports `lara_shell`.
