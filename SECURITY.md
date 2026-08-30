# Security

LLM-Telemetry is designed for a trusted closed network. It does not provide
authentication or authorization, and its dashboard, provider settings, API,
event stream, and screenshot upload endpoints must not be exposed directly to
the public internet.

If remote access is required, place the application behind an authenticated
reverse proxy, restrict network access with a firewall or VPN, and review the
configuration for your environment.

Please report suspected vulnerabilities privately to the repository owner
before opening a public issue.
