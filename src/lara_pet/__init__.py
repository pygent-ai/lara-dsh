"""lara_pet — the desktop pet application layer: composes lara_sdk and lara_shell.

The sidecar (``lara-pet-sidecar``) owns the pet's single realtime lara session
and the resident dsh daemon; the Tauri shell under ``pet/`` spawns it.
"""
