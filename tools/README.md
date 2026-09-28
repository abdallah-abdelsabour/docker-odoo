# Odoo RPC debug helper

`odoo_rpc.py` is a single-file tool (Python 3.8+, no dependencies) for inspecting and fixing
configuration on a live Odoo 19 instance over RPC.

- **Odoo 19**: uses the External JSON-2 API (`POST /json/2/<model>/<method>`, API key as a bearer token).
- **Odoo ≤ 18**: falls back to `/jsonrpc` `execute_kw` automatically (needs `ODOO_LOGIN`).

## Setup

```bash
cp tools/.env.example tools/.env   # then fill in URL, DB and API key
python3 tools/odoo_rpc.py info
```

Create the API key in Odoo under *Preferences → Account Security → New API Key*. RPC calls run with
that user's rights, so use an admin user if you need full access.

## Configuration debugging commands

| Command | What it shows |
|---|---|
| `info` | version, db, user, companies, installed or pending modules, base URL, subscription params |
| `params [filter]` / `set-param KEY VALUE` | system parameters (`ir.config_parameter`) |
| `settings [filter] [--all]` | current Settings screen values (`res.config.settings`) |
| `companies` | company fiscal, tax and currency setup |
| `modules [filter] [--state all]` | modules and their states/versions |
| `crons [--failing] [--all]` / `run-cron ID\|XMLID` | scheduled actions, failure counts, run now |
| `mail` | outgoing/incoming mail servers, mail params, `mail.mail` queue by state |
| `logs [filter] [--level ERROR]` | `ir.logging` (server actions, crons, `log_db`) |
| `access MODEL [--user ID]` | ACLs and record rules for a model, and which ones apply to a user |
| `fields MODEL [filter]` | field definitions |
| `xmlid module.name` | resolve an xmlid and read the record |
| `search`, `count`, `read` | generic data inspection (domains in JSON or Python syntax) |
| `call MODEL METHOD [--ids] [--kwargs]` | call **any** public method (full access) |
| `shell` | Python REPL with a connected `odoo` client |

Global flags: `--json`, `-v` (dump raw requests/responses), `--company ID` (repeatable,
sets `allowed_company_ids`), `--lang`, `--yes` (skip the confirmation for write methods), `--insecure`.

### Examples

```bash
./tools/odoo_rpc.py settings stock
./tools/odoo_rpc.py crons --failing
./tools/odoo_rpc.py access account.move --user 7
./tools/odoo_rpc.py --company 2 search account.journal "[('type','=','bank')]" -f name,code,company_id
./tools/odoo_rpc.py call res.partner write --ids 12 --kwargs '{"vals": {"vat": "300000000000003"}}'
```

With JSON-2, all method arguments are named, so pass them with `--kwargs`. Common methods
(`search_read`, `read`, `write`, `get_param`, …) also accept `--args`.

Write-type methods (`create`, `write`, `unlink`, `set_param`, `action_*`, `button_*`, …) ask for
confirmation unless you pass `--yes`.

## Tests

```bash
python3 -m unittest discover -s tools/tests
```
