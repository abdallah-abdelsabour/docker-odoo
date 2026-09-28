#!/usr/bin/env python3
"""Odoo RPC helper for debugging configuration on an Odoo 19 instance.

Stdlib only (Python 3.8+). Talks to Odoo through:
  * the Odoo 19 External JSON-2 API  (POST /json/2/<model>/<method>, API key auth)
  * the legacy /jsonrpc endpoint       (execute_kw, login + password/API key)

The protocol is picked automatically ("auto"): JSON-2 first, falling back to
/jsonrpc when the server does not expose JSON-2 (Odoo <= 18).

Connection settings come from CLI flags, environment variables, or a .env file
(next to this script or in the current directory):

    ODOO_URL=https://mycompany.odoo.com
    ODOO_DB=mycompany-main-123456
    ODOO_API_KEY=xxxxxxxxxxxxxxxx      # Preferences > Account Security > New API Key
    ODOO_LOGIN=admin                   # only needed for the legacy /jsonrpc protocol
    ODOO_PROTOCOL=auto                 # auto | json2 | jsonrpc

Examples:
    odoo_rpc.py info
    odoo_rpc.py params mail
    odoo_rpc.py settings --filter stock
    odoo_rpc.py crons --failing
    odoo_rpc.py logs --level ERROR --limit 20
    odoo_rpc.py access sale.order --user 7
    odoo_rpc.py search res.partner "[('is_company','=',True)]" -f name,vat -l 5
    odoo_rpc.py call res.partner write --ids 12 --kwargs '{"vals": {"name": "X"}}' --yes
    odoo_rpc.py shell
"""

import argparse
import ast
import json
import os
import re
import ssl
import sys
import urllib.error
import urllib.request

# Methods that change data. `call` asks for confirmation before running them.
WRITE_METHODS = {
    "create", "write", "unlink", "copy", "set_param", "execute",
    "button_immediate_install", "button_immediate_upgrade",
    "button_immediate_uninstall", "method_direct_trigger", "toggle_active",
    "action_archive", "action_unarchive",
}


class OdooRPCError(Exception):
    def __init__(self, message, name=None, debug=None, status=None):
        super().__init__(message)
        self.name = name
        self.debug = debug
        self.status = status

    def __str__(self):
        head = f"{self.name}: " if self.name else ""
        status = f" [HTTP {self.status}]" if self.status else ""
        return f"{head}{self.args[0]}{status}"


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
def load_dotenv(paths):
    """Load KEY=VALUE lines into os.environ without overriding existing vars."""
    for path in paths:
        if not os.path.isfile(path):
            continue
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                value = value.strip().strip('"').strip("'")
                os.environ.setdefault(key.strip(), value)


# --------------------------------------------------------------------------- #
# Client
# --------------------------------------------------------------------------- #
class OdooClient:
    def __init__(self, url, db, api_key=None, login=None, password=None,
                 protocol="auto", timeout=60, verbose=False, insecure=False,
                 context=None):
        if not url:
            raise OdooRPCError("ODOO_URL is not set")
        self.url = url.rstrip("/")
        self.db = db
        self.api_key = api_key
        self.login = login
        self.password = password or api_key
        self.timeout = timeout
        self.verbose = verbose
        self.context = context or {}
        self._ssl = ssl._create_unverified_context() if insecure else None
        self._uid = None
        self.protocol = protocol if protocol != "auto" else self._detect_protocol()

    # -- low level HTTP ---------------------------------------------------- #
    def _post(self, path, payload, headers=None):
        body = json.dumps(payload).encode()
        hdrs = {"Content-Type": "application/json; charset=utf-8"}
        hdrs.update(headers or {})
        req = urllib.request.Request(self.url + path, data=body, headers=hdrs, method="POST")
        if self.verbose:
            print(f">>> POST {path} {json.dumps(payload, default=str)[:2000]}", file=sys.stderr)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout, context=self._ssl) as resp:
                raw = resp.read()
                status = resp.status
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            status = exc.code
        except urllib.error.URLError as exc:
            raise OdooRPCError(f"cannot reach {self.url}: {exc.reason}") from exc
        if self.verbose:
            print(f"<<< {status} {raw[:2000].decode(errors='replace')}", file=sys.stderr)
        try:
            data = json.loads(raw) if raw else None
        except ValueError:
            snippet = raw[:300].decode(errors="replace")
            raise OdooRPCError(f"non-JSON response: {snippet}", status=status)
        return status, data

    def _detect_protocol(self):
        if not self.api_key:
            return "jsonrpc"
        # context_get is a cheap, always-allowed call on res.users.
        try:
            status, data = self._post("/json/2/res.users/context_get", {}, self._json2_headers())
        except OdooRPCError as exc:
            if exc.status == 404:  # Odoo <= 18: no JSON-2 route, HTML 404 page
                return "jsonrpc"
            raise
        if status == 404 and not (isinstance(data, dict) and data.get("name")):
            return "jsonrpc"
        return "json2"

    # -- JSON-2 (Odoo 19+) ------------------------------------------------- #
    def _json2_headers(self):
        hdrs = {"Authorization": f"bearer {self.api_key}"}
        if self.db:
            hdrs["X-Odoo-Database"] = self.db
        return hdrs

    def _call_json2(self, model, method, ids, args, kwargs):
        payload = dict(kwargs or {})
        if args:
            # JSON-2 only takes named arguments; map the common positional ones.
            payload.update(_positional_to_named(method, args))
        if ids is not None:
            payload["ids"] = list(ids)
        ctx = {**self.context, **payload.get("context", {})}
        if ctx:
            payload["context"] = ctx
        status, data = self._post(f"/json/2/{model}/{method}", payload, self._json2_headers())
        if status >= 400:
            if isinstance(data, dict):
                raise OdooRPCError(data.get("message") or str(data), name=data.get("name"),
                                   debug=data.get("debug"), status=status)
            raise OdooRPCError(str(data), status=status)
        return data

    # -- legacy /jsonrpc ---------------------------------------------------- #
    def _jsonrpc(self, service, method, *args):
        payload = {"jsonrpc": "2.0", "method": "call", "id": 1,
                   "params": {"service": service, "method": method, "args": list(args)}}
        status, data = self._post("/jsonrpc", payload)
        if not isinstance(data, dict):
            raise OdooRPCError(f"unexpected response: {data!r}", status=status)
        if data.get("error"):
            err = data["error"]
            info = err.get("data") or {}
            raise OdooRPCError(info.get("message") or err.get("message"),
                               name=info.get("name"), debug=info.get("debug"), status=status)
        return data.get("result")

    @property
    def uid(self):
        if self._uid is None:
            if not (self.db and self.login and self.password):
                raise OdooRPCError("legacy /jsonrpc needs ODOO_DB, ODOO_LOGIN and ODOO_PASSWORD/ODOO_API_KEY")
            self._uid = self._jsonrpc("common", "authenticate", self.db, self.login, self.password, {})
            if not self._uid:
                raise OdooRPCError("authentication failed (check db / login / password)")
        return self._uid

    def _call_jsonrpc(self, model, method, ids, args, kwargs):
        args = list(args or [])
        if ids is not None:
            args.insert(0, list(ids))
        kwargs = dict(kwargs or {})
        ctx = {**self.context, **kwargs.get("context", {})}
        if ctx:
            kwargs["context"] = ctx
        return self._jsonrpc("object", "execute_kw", self.db, self.uid, self.password,
                             model, method, args, kwargs)

    # -- public API ------------------------------------------------------- #
    def call(self, model, method, *args, ids=None, **kwargs):
        """Call any public model method. `ids` makes it a recordset call."""
        fn = self._call_json2 if self.protocol == "json2" else self._call_jsonrpc
        return fn(model, method, ids, args, kwargs)

    def version(self):
        try:
            return self._jsonrpc("common", "version")
        except OdooRPCError:
            status, data = self._post("/web/webclient/version_info", {"jsonrpc": "2.0", "params": {}})
            return (data or {}).get("result")

    def search_read(self, model, domain=None, fields=None, limit=None, order=None, offset=0):
        kw = {"domain": domain or [], "fields": fields or [], "offset": offset}
        if limit:
            kw["limit"] = limit
        if order:
            kw["order"] = order
        return self.call(model, "search_read", **kw)

    def search_count(self, model, domain=None):
        return self.call(model, "search_count", domain=domain or [])

    def read(self, model, ids, fields=None):
        return self.call(model, "read", ids=ids, fields=fields or [])

    def fields_get(self, model, attributes=("string", "type", "relation", "required", "readonly")):
        return self.call(model, "fields_get", attributes=list(attributes))

    def existing_fields(self, model, wanted):
        """Keep only the fields that exist on `model` (they move between versions)."""
        available = self.fields_get(model, attributes=("type",))
        return [f for f in wanted if f in available]

    def ref(self, xmlid):
        module, name = xmlid.split(".", 1)
        rows = self.search_read("ir.model.data", [("module", "=", module), ("name", "=", name)],
                                ["model", "res_id"], limit=1)
        if not rows:
            raise OdooRPCError(f"xmlid {xmlid} not found")
        return rows[0]["model"], rows[0]["res_id"]


_POSITIONAL_NAMES = {
    "search": ["domain"], "search_read": ["domain", "fields"], "search_count": ["domain"],
    "read": ["fields"], "write": ["vals"], "create": ["vals_list"],
    "fields_get": ["allfields"], "name_search": ["name"], "default_get": ["fields"],
    "get_param": ["key"], "set_param": ["key", "value"],
    "read_group": ["domain", "fields", "groupby"],
    "formatted_read_group": ["domain", "groupby", "aggregates"],
    "check_access_rights": ["operation"], "has_access": ["operation"],
    "check_access": ["operation"],
}


def _positional_to_named(method, args):
    names = _POSITIONAL_NAMES.get(method)
    if not names or len(args) > len(names):
        raise OdooRPCError(
            f"JSON-2 only accepts named arguments; pass --kwargs for {method!r} "
            f"(or use --protocol jsonrpc)")
    return dict(zip(names, args))


# --------------------------------------------------------------------------- #
# Output helpers
# --------------------------------------------------------------------------- #
def _fmt(value):
    if isinstance(value, list) and len(value) == 2 and isinstance(value[0], int) and isinstance(value[1], str):
        return f"{value[1]} ({value[0]})"
    if value is False:
        return ""
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False, default=str)
    return str(value)


def print_table(rows, columns=None, max_width=60):
    if not rows:
        print("(no records)")
        return
    columns = columns or list(rows[0].keys())
    cells = [[_fmt(r.get(c))[:max_width] for c in columns] for r in rows]
    widths = [max(len(c), *(len(row[i]) for row in cells)) for i, c in enumerate(columns)]
    print("  ".join(c.ljust(w) for c, w in zip(columns, widths)))
    print("  ".join("-" * w for w in widths))
    for row in cells:
        print("  ".join(v.ljust(w) for v, w in zip(row, widths)))
    print(f"({len(rows)} row{'s' if len(rows) != 1 else ''})")


def emit(args, data, columns=None):
    if args.json:
        print(json.dumps(data, indent=2, ensure_ascii=False, default=str))
    elif isinstance(data, list) and data and isinstance(data[0], dict):
        print_table(data, columns)
    elif isinstance(data, dict):
        width = max((len(str(k)) for k in data), default=0)
        for key in sorted(data):
            print(f"{str(key).ljust(width)}  {_fmt(data[key])}")
    else:
        print(json.dumps(data, indent=2, ensure_ascii=False, default=str))


def parse_literal(text, what):
    """Accept JSON or Python literals (so Odoo-style domains with tuples work)."""
    if text is None:
        return None
    try:
        return json.loads(text)
    except ValueError:
        try:
            return ast.literal_eval(text)
        except (ValueError, SyntaxError) as exc:
            raise OdooRPCError(f"cannot parse {what}: {text!r} ({exc})")


def parse_ids(text):
    return [int(x) for x in re.split(r"[,\s]+", text.strip()) if x]


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #
def cmd_info(client, args):
    ver = client.version() or {}
    ctx = client.call("res.users", "context_get")
    if client.protocol == "jsonrpc":
        user_domain = [("id", "=", client.uid)]
    elif client.login:
        user_domain = [("login", "=", client.login)]
    else:
        user_domain = None  # JSON-2 has no "who am I" call; set ODOO_LOGIN to show the user
    user = client.search_read("res.users", user_domain, ["name", "login", "company_id"], limit=1) \
        if user_domain else []
    user = user[0] if user else {}
    uid = user.get("id")
    companies = client.search_read("res.company", [], ["name", "currency_id", "country_id"])
    installed = client.search_count("ir.module.module", [("state", "=", "installed")])
    pending = client.search_read("ir.module.module",
                                 [("state", "in", ["to install", "to upgrade", "to remove"])],
                                 ["name", "state"])
    info = {
        "url": client.url, "database": client.db, "protocol": client.protocol,
        "server_version": ver.get("server_version"),
        "user": f"{user.get('name')} <{user.get('login')}> (id {uid})",
        "company": _fmt(user.get("company_id")), "lang": ctx.get("lang"), "tz": ctx.get("tz"),
        "companies": ", ".join(f"{c['name']} ({c['id']})" for c in companies),
        "installed_modules": installed,
        "pending_module_ops": ", ".join(f"{m['name']}:{m['state']}" for m in pending) or "none",
    }
    for key in ("web.base.url", "web.base.url.freeze", "database.uuid", "database.expiration_date",
                "database.enterprise_code"):
        info[key] = client.call("ir.config_parameter", "get_param", key=key)
    emit(args, info)


def cmd_modules(client, args):
    domain = [("state", "=", args.state)] if args.state != "all" else []
    if args.filter:
        domain += ["|", ("name", "ilike", args.filter), ("shortdesc", "ilike", args.filter)]
    rows = client.search_read("ir.module.module", domain,
                              ["name", "shortdesc", "state", "installed_version", "latest_version", "author"],
                              order="name")
    emit(args, rows, ["name", "state", "installed_version", "latest_version", "shortdesc", "author"])


def cmd_params(client, args):
    domain = [("key", "ilike", args.filter)] if args.filter else []
    rows = client.search_read("ir.config_parameter", domain, ["key", "value", "write_date"], order="key")
    emit(args, rows, ["key", "value", "write_date"])


def cmd_set_param(client, args):
    old = client.call("ir.config_parameter", "get_param", key=args.key)
    print(f"{args.key}: {old!r} -> {args.value!r}")
    if not confirm(args, "write this system parameter?"):
        return
    client.call("ir.config_parameter", "set_param", key=args.key, value=args.value)
    print("done")


def cmd_settings(client, args):
    """Current values of the Settings screen (res.config.settings.default_get)."""
    fields = client.fields_get("res.config.settings", attributes=("string", "type"))
    names = [n for n, f in fields.items()
             if f["type"] not in ("one2many", "binary")
             and (not args.filter or args.filter.lower() in (n + f.get("string", "")).lower())]
    try:  # the argument is `fields` in recent versions, `fields_list` before
        values = client.call("res.config.settings", "default_get", fields=names)
    except OdooRPCError:
        values = client.call("res.config.settings", "default_get", fields_list=names)
    rows = [{"field": n, "label": fields[n].get("string"), "type": fields[n]["type"], "value": values.get(n)}
            for n in sorted(names) if args.all or values.get(n) not in (False, None, "", [])]
    emit(args, rows, ["field", "label", "type", "value"])


def cmd_companies(client, args):
    wanted = ["name", "currency_id", "country_id", "vat", "company_registry", "parent_id",
              "fiscalyear_last_month", "fiscalyear_last_day", "account_fiscal_country_id",
              "chart_template", "tax_calculation_rounding_method", "account_sale_tax_id",
              "account_purchase_tax_id", "email", "phone"]
    rows = client.search_read("res.company", [], client.existing_fields("res.company", wanted))
    emit(args, rows)


def cmd_crons(client, args):
    wanted = ["name", "active", "interval_number", "interval_type", "nextcall", "lastcall",
              "priority", "failure_count", "first_failure_date", "user_id"]
    domain = [] if args.all else [("active", "=", True)]
    fields = client.existing_fields("ir.cron", wanted)
    if args.failing:
        if "failure_count" not in fields:
            raise OdooRPCError("this Odoo version has no ir.cron.failure_count")
        domain.append(("failure_count", ">", 0))
    rows = client.call("ir.cron", "search_read", domain=domain, fields=fields,
                       order="nextcall", context={"active_test": False})
    emit(args, rows, fields)


def cmd_run_cron(client, args):
    cron_id = args.cron
    if not cron_id.isdigit():
        _, cron_id = client.ref(cron_id)
    if not confirm(args, f"run ir.cron {cron_id} now?"):
        return
    emit(args, client.call("ir.cron", "method_direct_trigger", ids=[int(cron_id)]))


def cmd_mail(client, args):
    print("== Outgoing mail servers (ir.mail_server)")
    wanted = ["name", "smtp_host", "smtp_port", "smtp_encryption", "smtp_authentication", "smtp_user",
              "from_filter", "sequence", "active"]
    emit(args, client.search_read("ir.mail_server", [], client.existing_fields("ir.mail_server", wanted)))
    try:
        rows = client.search_read("fetchmail.server", [], ["name", "server_type", "server", "port",
                                                           "is_ssl", "state", "date", "active"])
        print("\n== Incoming mail servers (fetchmail.server)")
        emit(args, rows)
    except OdooRPCError:
        pass
    print("\n== Mail parameters")
    params = client.search_read("ir.config_parameter",
                                ["|", "|", ("key", "ilike", "mail."), ("key", "=", "web.base.url"),
                                 ("key", "ilike", "catchall")], ["key", "value"], order="key")
    emit(args, params)
    print("\n== Outgoing queue (mail.mail) by state")
    states = client.fields_get("mail.mail", attributes=("selection",))["state"]["selection"]
    emit(args, [{"state": key, "count": client.search_count("mail.mail", [("state", "=", key)])}
                for key, _label in states])


def cmd_logs(client, args):
    domain = [("level", "=", args.level)] if args.level else []
    if args.filter:
        domain += ["|", ("name", "ilike", args.filter), ("message", "ilike", args.filter)]
    rows = client.search_read("ir.logging", domain,
                              ["create_date", "level", "name", "func", "line", "message"],
                              limit=args.limit, order="id desc")
    if args.json:
        emit(args, rows)
        return
    for r in rows:
        print(f"[{r['create_date']}] {r['level']} {r['name']}:{r['func']}:{r['line']}")
        print("   " + (r["message"] or "").replace("\n", "\n   "))
    if not rows:
        print("(no ir.logging entries — server logs go to the log file, not the DB, unless log_db is set)")


def cmd_fields(client, args):
    attrs = ("string", "type", "relation", "required", "readonly", "store", "compute", "company_dependent")
    fields = client.fields_get(args.model, attributes=attrs)
    rows = [{"name": n, **f} for n, f in sorted(fields.items())
            if not args.filter or args.filter.lower() in (n + f.get("string", "")).lower()]
    emit(args, rows, ["name", "string", "type", "relation", "required", "readonly", "store"])


def cmd_access(client, args):
    ctx = {}
    model_rows = client.search_read("ir.model", [("model", "=", args.model)], ["id", "name"], limit=1)
    if not model_rows:
        raise OdooRPCError(f"model {args.model} not found")
    model_id = model_rows[0]["id"]
    print(f"== {args.model} ({model_rows[0]['name']})")

    if args.user:
        user = client.read("res.users", [args.user], ["name", "login", "company_ids", "company_id"])[0]
        ctx = {"allowed_company_ids": user["company_ids"]}
        print(f"user: {user['name']} <{user['login']}>  (checking ACLs for this user's groups)")
        # Odoo 19: all_group_ids (incl. implied groups); older: groups_id
        group_field = client.existing_fields("res.users", ["all_group_ids", "groups_id"])[0]
        group_ids = set(client.read("res.users", [args.user], [group_field])[0][group_field])
    else:
        print("current RPC user:")
        for op in ("read", "write", "create", "unlink"):
            try:
                ok = client.call(args.model, "has_access", operation=op)
            except OdooRPCError:
                try:
                    ok = client.call(args.model, "check_access_rights", operation=op, raise_exception=False)
                except OdooRPCError as exc:
                    ok = f"? ({exc})"
            print(f"  {op:<7} {ok}")
        group_ids = None

    print("\n== Access rights (ir.model.access)")
    acls = client.call("ir.model.access", "search_read", domain=[("model_id", "=", model_id)],
                       fields=["name", "group_id", "perm_read", "perm_write", "perm_create", "perm_unlink",
                               "active"], context={"active_test": False, **ctx})
    for acl in acls:
        gid = acl["group_id"][0] if acl["group_id"] else None
        acl["applies"] = "global" if gid is None else ("yes" if group_ids is None or gid in group_ids else "no")
    emit(args, acls, ["name", "group_id", "perm_read", "perm_write", "perm_create", "perm_unlink",
                      "active", "applies"])

    print("\n== Record rules (ir.rule)")
    rules = client.call("ir.rule", "search_read", domain=[("model_id", "=", model_id)],
                        fields=["name", "groups", "domain_force", "perm_read", "perm_write", "perm_create",
                                "perm_unlink", "active", "global"], context={"active_test": False})
    for rule in rules:
        rule["applies"] = "global" if rule["global"] else (
            "yes" if group_ids is None or group_ids & set(rule["groups"]) else "no")
    emit(args, rules, ["name", "domain_force", "groups", "perm_read", "perm_write", "perm_create",
                       "perm_unlink", "active", "applies"])


def cmd_xmlid(client, args):
    model, res_id = client.ref(args.xmlid)
    rec = client.read(model, [res_id], args.fields.split(",") if args.fields else [])
    print(f"{args.xmlid} -> {model}({res_id})")
    emit(args, rec[0] if rec else {})


def cmd_search(client, args):
    domain = parse_literal(args.domain, "domain") if args.domain else []
    fields = args.fields.split(",") if args.fields else ["display_name"]
    rows = client.call(args.model, "search_read", domain=domain, fields=fields, limit=args.limit,
                       order=args.order, context={"active_test": not args.archived})
    emit(args, rows, ["id"] + [f for f in fields if f != "id"])


def cmd_count(client, args):
    domain = parse_literal(args.domain, "domain") if args.domain else []
    print(client.search_count(args.model, domain))


def cmd_read(client, args):
    rows = client.read(args.model, parse_ids(args.ids), args.fields.split(",") if args.fields else [])
    if args.json or len(rows) != 1:
        emit(args, rows)
    else:
        emit(args, rows[0])


def cmd_call(client, args):
    pos = parse_literal(args.args, "--args") or []
    kwargs = parse_literal(args.kwargs, "--kwargs") or {}
    ids = parse_ids(args.ids) if args.ids else None
    if args.method in WRITE_METHODS or args.method.startswith(("action_", "button_")):
        target = f"{args.model}{tuple(ids) if ids else ''}.{args.method}"
        if not confirm(args, f"{target} may modify data, run it?"):
            return
    emit(args, client.call(args.model, args.method, *pos, ids=ids, **kwargs))


def cmd_shell(client, args):
    import code
    banner = (f"Connected to {client.url} db={client.db} protocol={client.protocol}\n"
              "Objects: odoo (OdooClient), pp (pretty print)\n"
              "  odoo.call('res.partner', 'search_read', domain=[], fields=['name'], limit=5)\n"
              "  odoo.call('res.partner', 'write', ids=[1], vals={'name': 'X'})\n"
              "  odoo.search_read(model, domain, fields), odoo.read(model, ids), odoo.ref('base.main_company')")
    ns = {"odoo": client, "pp": lambda d: print(json.dumps(d, indent=2, ensure_ascii=False, default=str))}
    code.interact(banner=banner, local=ns)


def confirm(args, question):
    if args.yes:
        return True
    if not sys.stdin.isatty():
        print(f"refusing: {question} (pass --yes to confirm non-interactively)", file=sys.stderr)
        return False
    return input(f"{question} [y/N] ").strip().lower() in ("y", "yes")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def build_parser():
    p = argparse.ArgumentParser(description="Odoo 19 RPC debugging helper",
                                formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    p.add_argument("--url", help="Odoo base URL (env ODOO_URL)")
    p.add_argument("--db", help="database name (env ODOO_DB)")
    p.add_argument("--api-key", help="API key (env ODOO_API_KEY)")
    p.add_argument("--login", help="login for legacy /jsonrpc (env ODOO_LOGIN)")
    p.add_argument("--password", help="password for legacy /jsonrpc (env ODOO_PASSWORD, defaults to API key)")
    p.add_argument("--protocol", choices=["auto", "json2", "jsonrpc"], help="env ODOO_PROTOCOL (default auto)")
    p.add_argument("--env-file", help="extra .env file to load")
    p.add_argument("--company", type=int, action="append",
                   help="allowed company id (repeatable; first is the active one)")
    p.add_argument("--lang", help="context lang, e.g. en_US / ar_001")
    p.add_argument("--timeout", type=int, default=60)
    p.add_argument("--insecure", action="store_true", help="skip TLS verification (self-signed dev servers)")
    p.add_argument("-v", "--verbose", action="store_true", help="dump raw requests/responses to stderr")
    p.add_argument("--json", action="store_true", help="JSON output instead of tables")
    p.add_argument("-y", "--yes", action="store_true", help="don't ask before write operations")

    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("info", help="server version, db, user, companies, module state, key params")

    s = sub.add_parser("modules", help="list modules")
    s.add_argument("filter", nargs="?")
    s.add_argument("--state", default="installed",
                   help="installed | uninstalled | to upgrade | to install | to remove | all")

    s = sub.add_parser("params", help="system parameters (ir.config_parameter)")
    s.add_argument("filter", nargs="?")

    s = sub.add_parser("set-param", help="write a system parameter")
    s.add_argument("key")
    s.add_argument("value")

    s = sub.add_parser("settings", help="current Settings screen values (res.config.settings)")
    s.add_argument("filter", nargs="?")
    s.add_argument("--all", action="store_true", help="include empty/False values")

    sub.add_parser("companies", help="company configuration")

    s = sub.add_parser("crons", help="scheduled actions")
    s.add_argument("--failing", action="store_true", help="only crons with failure_count > 0")
    s.add_argument("--all", action="store_true", help="include inactive crons")

    s = sub.add_parser("run-cron", help="trigger a scheduled action now")
    s.add_argument("cron", help="ir.cron id or xmlid")

    sub.add_parser("mail", help="mail servers, mail params, outgoing queue")

    s = sub.add_parser("logs", help="ir.logging entries (server actions, crons, log_db)")
    s.add_argument("filter", nargs="?")
    s.add_argument("--level", help="ERROR / WARNING / INFO ...")
    s.add_argument("-l", "--limit", type=int, default=30)

    s = sub.add_parser("fields", help="model fields")
    s.add_argument("model")
    s.add_argument("filter", nargs="?")

    s = sub.add_parser("access", help="ACLs and record rules for a model")
    s.add_argument("model")
    s.add_argument("--user", type=int, help="evaluate against this res.users id's groups")

    s = sub.add_parser("xmlid", help="resolve an xmlid and read the record")
    s.add_argument("xmlid")
    s.add_argument("-f", "--fields")

    s = sub.add_parser("search", help="search_read")
    s.add_argument("model")
    s.add_argument("domain", nargs="?", help="JSON or Python literal, e.g. \"[('state','=','draft')]\"")
    s.add_argument("-f", "--fields", help="comma separated")
    s.add_argument("-l", "--limit", type=int, default=20)
    s.add_argument("-o", "--order")
    s.add_argument("--archived", action="store_true", help="include archived records")

    s = sub.add_parser("count", help="search_count")
    s.add_argument("model")
    s.add_argument("domain", nargs="?")

    s = sub.add_parser("read", help="read records by id")
    s.add_argument("model")
    s.add_argument("ids", help="e.g. 1,2,3")
    s.add_argument("-f", "--fields")

    s = sub.add_parser("call", help="call any model method (full access)")
    s.add_argument("model")
    s.add_argument("method")
    s.add_argument("--ids", help="record ids for recordset methods, e.g. 1,2")
    s.add_argument("--args", help="positional args as JSON list (jsonrpc; mapped for common methods on json2)")
    s.add_argument("--kwargs", help="named args as JSON object")

    sub.add_parser("shell", help="interactive Python shell with a connected client")
    return p


COMMANDS = {
    "info": cmd_info, "modules": cmd_modules, "params": cmd_params, "set-param": cmd_set_param,
    "settings": cmd_settings, "companies": cmd_companies, "crons": cmd_crons, "run-cron": cmd_run_cron,
    "mail": cmd_mail, "logs": cmd_logs, "fields": cmd_fields, "access": cmd_access, "xmlid": cmd_xmlid,
    "search": cmd_search, "count": cmd_count, "read": cmd_read, "call": cmd_call, "shell": cmd_shell,
}


def client_from_args(args):
    here = os.path.dirname(os.path.abspath(__file__))
    load_dotenv([p for p in (args.env_file, os.path.join(os.getcwd(), ".env"), os.path.join(here, ".env")) if p])
    env = os.environ.get
    context = {}
    if args.company:
        context["allowed_company_ids"] = args.company
    if args.lang:
        context["lang"] = args.lang
    return OdooClient(
        url=args.url or env("ODOO_URL"),
        db=args.db or env("ODOO_DB"),
        api_key=args.api_key or env("ODOO_API_KEY"),
        login=args.login or env("ODOO_LOGIN"),
        password=args.password or env("ODOO_PASSWORD"),
        protocol=args.protocol or env("ODOO_PROTOCOL", "auto"),
        timeout=args.timeout, verbose=args.verbose, insecure=args.insecure, context=context,
    )


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        client = client_from_args(args)
        COMMANDS[args.cmd](client, args)
    except OdooRPCError as exc:
        print(f"error: {exc}", file=sys.stderr)
        if exc.debug and args.verbose:
            print(exc.debug, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
