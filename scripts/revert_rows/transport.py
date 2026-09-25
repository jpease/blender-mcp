"""
Rows guarding the ported HTTP transport and its host, port and opt-in validation.

Label prefix: `transport:`.
"""

from .common import CLIT, SERVER_CLI, Revert

ROWS: list[Revert] = [
    # --- the ported HTTP transport (ee25ffc) ---
    Revert(
        "transport: HTTP becomes the default, so every existing stdio client config breaks",
        SERVER_CLI,
        'env.get(TRANSPORT_ENV, "stdio")',
        'env.get(TRANSPORT_ENV, "http")',
        (
            f"{CLIT}::test_stdio_is_the_default",
            f"{CLIT}::test_http_settings_are_ignored_under_stdio",
            f"{CLIT}::test_main_serves_stdio_by_default",
        ),
    ),
    Revert(
        "transport: opting into HTTP alone binds every interface",
        SERVER_CLI,
        'DEFAULT_HTTP_HOST = "127.0.0.1"',
        'DEFAULT_HTTP_HOST = "0.0.0.0"',
        (f"{CLIT}::test_http_defaults_to_loopback_on_port_8000",),
    ),
    Revert(
        "transport: the default HTTP port is not the one the rig publishes",
        SERVER_CLI,
        "DEFAULT_HTTP_PORT = 8000",
        "DEFAULT_HTTP_PORT = 9000",
        (f"{CLIT}::test_http_defaults_to_loopback_on_port_8000",),
    ),
    Revert(
        "transport: the configured host and port are ignored",
        SERVER_CLI,
        "        return HttpConfig(host=_resolve_http_host(env), port=_parse_port(env))",
        "        return HttpConfig(host=DEFAULT_HTTP_HOST, port=DEFAULT_HTTP_PORT)",
        (f"{CLIT}::test_http_host_and_port_are_configurable",),
    ),
    Revert(
        "transport: the transport name is read raw, so compose's padding or casing is rejected",
        SERVER_CLI,
        'env.get(TRANSPORT_ENV, "stdio").strip().lower()',
        'env.get(TRANSPORT_ENV, "stdio")',
        (f"{CLIT}::test_transport_name_ignores_case_and_whitespace",),
    ),
    Revert(
        "transport: a misspelt transport silently serves stdio nobody reads",
        SERVER_CLI,
        "    raise ValueError(f\"{TRANSPORT_ENV} must be 'stdio' or 'http', got {name!r}\")",
        "    return StdioConfig()",
        (
            f"{CLIT}::test_unknown_transport_is_rejected",
            f"{CLIT}::test_main_reports_a_bad_transport_without_serving",
        ),
    ),
    Revert(
        "transport: the HTTP port is no longer range-checked before anything binds",
        SERVER_CLI,
        "    if not 1 <= port <= MAX_PORT:",
        "    if False:",
        (
            f"{CLIT}::test_invalid_http_port_is_rejected[abc]",
            f"{CLIT}::test_invalid_http_port_is_rejected[0]",
            f"{CLIT}::test_invalid_http_port_is_rejected[65536]",
            f"{CLIT}::test_invalid_http_port_is_rejected[-1]",
        ),
    ),
    Revert(
        "transport: main() never dispatches to HTTP, so the container serves stdio into EOF",
        SERVER_CLI,
        '    if config.transport == "streamable-http":',
        "    if False:",
        (f"{CLIT}::test_main_serves_http_on_the_configured_address",),
    ),
    Revert(
        "transport: the configured bind address is never applied before serving",
        SERVER_CLI,
        "    mcp.settings.host = host\n    mcp.settings.port = port\n",
        "",
        (f"{CLIT}::test_main_serves_http_on_the_configured_address",),
    ),
    Revert(
        "transport: binding 0.0.0.0 drops the DNS-rebinding protection FastMCP built in",
        SERVER_CLI,
        "    mcp.settings.host = host\n    mcp.settings.port = port\n",
        "    mcp.settings.host = host\n    mcp.settings.port = port\n    mcp.settings.transport_security = None\n",
        (f"{CLIT}::test_binding_all_interfaces_keeps_dns_rebinding_protection",),
    ),
    # --- HTTP host, port and opt-in validation ---
    Revert(
        "transport: an empty BLENDERMCP_HTTP_HOST falls through to a wildcard bind again",
        SERVER_CLI,
        "    host = raw.strip()\n    if not host:",
        "    host = raw.strip() or DEFAULT_HTTP_HOST\n    if False:",
        (
            f"{CLIT}::test_empty_http_host_is_rejected[]",
            f"{CLIT}::test_empty_http_host_is_rejected[ ]",
            f"{CLIT}::test_empty_http_host_is_rejected[\\t\\n]",
            f"{CLIT}::test_an_empty_host_is_rejected_even_with_remote_binds_allowed",
        ),
    ),
    Revert(
        "transport: a non-loopback bind needs no opt-in",
        SERVER_CLI,
        "    if not _allows_remote(env):",
        "    if False:",
        (
            f"{CLIT}::test_a_non_loopback_bind_is_refused_without_the_opt_in[0]",
            f"{CLIT}::test_a_non_loopback_bind_is_refused_without_the_opt_in[0.0.0.0]",
            f"{CLIT}::test_a_non_loopback_bind_is_refused_without_the_opt_in[::]",
            f"{CLIT}::test_a_non_loopback_bind_is_refused_without_the_opt_in[192.168.1.5]",
            f"{CLIT}::test_a_non_loopback_bind_is_refused_without_the_opt_in[example.internal]",
            f"{CLIT}::test_main_refuses_a_wildcard_bind_without_serving",
        ),
    ),
    Revert(
        "transport: loopback is not recognised, so even 127.0.0.1 demands consent",
        SERVER_CLI,
        'LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})',
        "LOOPBACK_HOSTS = frozenset()",
        (
            f"{CLIT}::test_loopback_hosts_need_no_opt_in[127.0.0.1]",
            f"{CLIT}::test_loopback_hosts_need_no_opt_in[localhost]",
            f"{CLIT}::test_loopback_hosts_need_no_opt_in[LOCALHOST]",
            f"{CLIT}::test_loopback_hosts_need_no_opt_in[::1]",
            f"{CLIT}::test_a_loopback_bind_is_not_warned_about",
        ),
    ),
    Revert(
        "transport: the opt-in is tested for presence, so ALLOW_REMOTE=false reads as consent",
        SERVER_CLI,
        '    return env.get(HTTP_ALLOW_REMOTE_ENV, "").strip().lower() in TRUTHY_VALUES',
        "    return HTTP_ALLOW_REMOTE_ENV in env",
        (
            f"{CLIT}::test_a_falsy_opt_in_does_not_open_the_bind[0]",
            f"{CLIT}::test_a_falsy_opt_in_does_not_open_the_bind[false]",
            f"{CLIT}::test_a_falsy_opt_in_does_not_open_the_bind[no]",
            f"{CLIT}::test_a_falsy_opt_in_does_not_open_the_bind[off]",
            f"{CLIT}::test_a_falsy_opt_in_does_not_open_the_bind[]",
            f"{CLIT}::test_a_falsy_opt_in_does_not_open_the_bind[ ]",
            f"{CLIT}::test_a_falsy_opt_in_does_not_open_the_bind[maybe]",
        ),
    ),
    Revert(
        "transport: no value opts in at all, so every spelling of yes is refused",
        SERVER_CLI,
        'TRUTHY_VALUES = frozenset({"1", "true", "yes", "on"})',
        "TRUTHY_VALUES = frozenset()",
        (
            f"{CLIT}::test_the_opt_in_honours_the_usual_spellings_of_yes[1]",
            f"{CLIT}::test_the_opt_in_honours_the_usual_spellings_of_yes[true]",
            f"{CLIT}::test_the_opt_in_honours_the_usual_spellings_of_yes[TRUE]",
            f"{CLIT}::test_the_opt_in_honours_the_usual_spellings_of_yes[on]",
            f"{CLIT}::test_the_opt_in_honours_the_usual_spellings_of_yes[ yes ]",
        ),
    ),
    Revert(
        "transport: an authorised wide bind is made silently, with nothing in the log",
        SERVER_CLI,
        "    logger.warning(\n"
        '        f"{HTTP_ALLOW_REMOTE_ENV} is set, so BlenderMCP will bind {host}, which {widening}. "',
        "    logger.debug(\n"
        '        f"{HTTP_ALLOW_REMOTE_ENV} is set, so BlenderMCP will bind {host}, which {widening}. "',
        (f"{CLIT}::test_an_allowed_non_loopback_bind_says_what_it_costs",),
    ),
    Revert(
        "transport: the port is parsed by int() alone, so underscores and full-width digits pass",
        SERVER_CLI,
        "    port = int(digits) if len(digits) <= MAX_PORT_DIGITS and digits.isascii() and digits.isdecimal() else 0",
        "    port = int(digits) if digits else 0",
        (
            f"{CLIT}::test_port_syntax_no_port_includes_is_rejected[8_000]",
            f"{CLIT}::test_port_syntax_no_port_includes_is_rejected[+8000]",
            f"{CLIT}::test_port_syntax_no_port_includes_is_rejected[\\uff11\\uff12\\uff13]",
            f"{CLIT}::test_port_syntax_no_port_includes_is_rejected[\\uff10\\uff10\\uff10\\uff18\\uff10\\uff10\\uff10]",
        ),
    ),
    Revert(
        "transport: the port length is unbounded, so CPython's own int() error escapes instead of ours",
        SERVER_CLI,
        "    port = int(digits) if len(digits) <= MAX_PORT_DIGITS and digits.isascii() and digits.isdecimal() else 0",
        "    port = int(digits) if digits.isascii() and digits.isdecimal() else 0",
        (f"{CLIT}::test_a_rejected_value_is_not_echoed_whole_into_the_log",),
    ),
    Revert(
        "transport: the port is not stripped, so a padded compose value is rejected",
        SERVER_CLI,
        "    digits = raw.strip()",
        "    digits = raw",
        (f"{CLIT}::test_padded_http_port_is_accepted",),
    ),
    Revert(
        "transport: a rejected value is echoed into the log whole, however long it is",
        SERVER_CLI,
        "    clipped = raw[:MAX_ECHOED_CHARS]",
        "    clipped = raw",
        (f"{CLIT}::test_a_rejected_value_is_not_echoed_whole_into_the_log",),
    ),
    Revert(
        "transport: stdio warns about ignored HTTP settings that were never set",
        SERVER_CLI,
        "    ignored = [name for name in HTTP_ONLY_ENVS if name in env]",
        "    ignored = list(HTTP_ONLY_ENVS)",
        (f"{CLIT}::test_stdio_without_http_settings_warns_about_nothing",),
    ),
    Revert(
        "transport: HttpConfig defaults its address again, so the illegal state is constructible",
        SERVER_CLI,
        "    host: str\n    port: int\n",
        "    host: str | None = None\n    port: int | None = None\n",
        (f"{CLIT}::test_an_http_config_without_an_address_cannot_be_built_even_under_o",),
    ),
]
