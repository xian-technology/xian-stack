"""Real PostgreSQL/PostGraphile contract; enabled by the image built in CI."""

from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
import subprocess
import time
from http.client import HTTPException
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import pytest

ROOT = Path(__file__).resolve().parents[1]
IMAGE = os.environ.get("XIAN_POSTGRAPHILE_TEST_IMAGE")
pytestmark = pytest.mark.skipif(not IMAGE, reason="build and set XIAN_POSTGRAPHILE_TEST_IMAGE")


def command(*args, **kwargs):
    return subprocess.run(args, check=True, capture_output=True, text=True, **kwargs).stdout.strip()


def wait_until(read):
    for _ in range(100):
        try:
            return read()
        # A reset connection means the server is still starting: Docker's port
        # proxy accepts before the process inside listens.
        except (subprocess.CalledProcessError, URLError, HTTPException, ConnectionError):
            time.sleep(0.2)
    raise AssertionError("isolated database/query service did not become ready")


def port(container, target):
    return command("docker", "port", container, target).rsplit(":", 1)[1]


def graphql(url, query, variables=None):
    request = Request(
        url,
        data=json.dumps({"query": query, "variables": variables or {}}).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urlopen(request, timeout=10) as response:
        result = json.load(response)
    assert "errors" not in result, result
    return result["data"]


async def seed_database(db_port, password_file, spool):
    from xian.services.bds.bds import BDS
    from xian.services.bds.config import BdsConfig

    bds = BDS(BdsConfig(port=int(db_port), password_file=str(password_file), spool_dir=str(spool)))
    await bds.open_storage()
    try:
        await bds.ensure_schema()
        await bds.db.execute((ROOT / "tests/fixtures/postgraphile.sql").read_text())
    finally:
        await bds.db.close_pool()


@pytest.fixture(scope="module")
def native_graphql(tmp_path_factory):
    directory = tmp_path_factory.mktemp("postgraphile")
    prefix = "xian-graphile-test-" + secrets.token_hex(6)
    database, server = prefix + "-db", prefix + "-http"
    password_file = directory / "password"
    password = secrets.token_hex(24)
    password_file.write_text(password)
    password_file.chmod(0o600)
    env_file = directory / "postgres.env"
    env_file.write_text(
        "POSTGRES_USER=xian\nPOSTGRES_DB=xian\nPOSTGRES_PASSWORD_FILE=/test-password\n"
    )
    env_file.chmod(0o600)
    command("docker", "network", "create", prefix)
    try:
        command(
            "docker",
            "run",
            "-d",
            "--name",
            database,
            "--network",
            prefix,
            "--network-alias",
            "postgres",
            "--env-file",
            str(env_file),
            "-v",
            f"{password_file}:/test-password:ro",
            "-p",
            "127.0.0.1::5432",
            "postgres:18.7",
        )
        wait_until(
            lambda: command(
                "docker", "exec", database, "psql", "-U", "xian", "-d", "xian", "-c", "SELECT 1"
            )
        )
        db_port = port(database, "5432/tcp")
        asyncio.run(seed_database(db_port, password_file, directory / "spool"))
        command(
            "docker",
            "exec",
            "-i",
            "-e",
            "PGHOST=localhost",
            "-e",
            "PGPORT=5432",
            "-e",
            "PGDATABASE=xian",
            "-e",
            "PGUSER=xian",
            "-e",
            "XIAN_BDS_PASSWORD_FILE=/test-password",
            "-e",
            "XIAN_POSTGRAPHILE_USER=xian_graphql",
            "-e",
            f"XIAN_POSTGRAPHILE_PASSWORD={password}",
            database,
            "bash",
            input=(ROOT / "docker/postgres/init-postgraphile-role.sh").read_text(),
        )
        env_file.write_text(
            f"PGUSER=xian_graphql\nPGPASSWORD={password}\nPGDATABASE=xian\nGRAPHILE_ENV=production\n"
        )
        command(
            "docker",
            "run",
            "-d",
            "--name",
            server,
            "--network",
            prefix,
            "--env-file",
            str(env_file),
            "-p",
            "127.0.0.1::5000",
            IMAGE,
            "/usr/src/app/start-postgraphile.sh",
        )
        url = f"http://127.0.0.1:{port(server, '5000/tcp')}/graphql"
        wait_until(lambda: graphql(url, "{ __typename }"))
        yield {"url": url, "database": database, "server": server}
    finally:
        for container in (server, database):
            subprocess.run(["docker", "rm", "-f", "-v", container], capture_output=True)
        subprocess.run(["docker", "network", "rm", prefix], capture_output=True)


def test_native_ids_json_and_cursor_pagination(native_graphql):
    url = native_graphql["url"]
    first = graphql(
        url,
        """{
      allEvents(first: 1, orderBy: ROW_ID_ASC) {
        edges { cursor node { id rowId data transactionByTxHash { blockHeight } } }
        pageInfo { hasNextPage }
      }
    }""",
    )["allEvents"]
    node = first["edges"][0]["node"]
    assert node["rowId"] == "9007199254740993"
    assert node["id"] != node["rowId"]
    assert node["data"] == {"amount": {"__fixed__": "1.5"}}
    assert node["transactionByTxHash"] == {"blockHeight": "1"}
    assert first["pageInfo"]["hasNextPage"] is True
    second = graphql(
        url,
        """query Next($cursor: Cursor!) {
      allEvents(first: 1, after: $cursor, orderBy: ROW_ID_ASC) { nodes { rowId } }
    }""",
        {"cursor": first["edges"][0]["cursor"]},
    )
    assert second["allEvents"]["nodes"] == [{"rowId": "9007199254740994"}]
    filtered = graphql(
        url,
        """query ToAddress($to: String!) {
      allEvents(filter: {dataIndexed: {contains: {to: $to}}}) { nodes { rowId } }
    }""",
        {"to": "bob"},
    )
    assert filtered["allEvents"]["nodes"] == [{"rowId": "9007199254740993"}]


def test_sdk_event_queries_execute_with_precise_after_id(native_graphql):
    from xian_py import XianAsync

    async def events():
        async with XianAsync(native_graphql["url"], chain_id="fixture") as client:
            first = await client._graphql_list_events(
                "currency", "Transfer", limit=1, offset=0, after_id=None
            )
            following = await client._graphql_list_events(
                "currency", "Transfer", limit=2, offset=0, after_id=first[0].id
            )
            by_tx = await client._graphql_get_events_for_tx("tx-1")
            return first, following, by_tx

    first, following, by_tx = asyncio.run(events())
    assert first[0].id == 9007199254740993
    assert [item.id for item in following] == [9007199254740994]
    assert len(by_tx) == 3
    assert first[0].block_height == 1
    assert first[0].data == {"amount": {"__fixed__": "1.5"}}


def test_catalog_filter_state_relations_and_aggregates(native_graphql):
    result = graphql(
        native_graphql["url"],
        """{
      allContracts(condition: {xsc001: true}) { nodes { name } }
      allStates(filter: {key: {endsWith: ".balances:bob"}}) { nodes { value } }
      transactionByHash(hash: "tx-1") {
        stateChangesByTxHash { nodes { newValue transactionByTxHash { blockHeight } } }
      }
      allRewards(condition: {type: "developer", recipientKey: "alice"}) {
        aggregates { sum { value } }
      }
    }""",
    )
    assert result["allContracts"]["nodes"] == [{"name": "currency"}]
    assert result["allStates"]["nodes"] == [{"value": {"__fixed__": "1.5"}}]
    assert result["transactionByHash"]["stateChangesByTxHash"]["nodes"] == [
        {"newValue": {"__fixed__": "1.5"}, "transactionByTxHash": {"blockHeight": "1"}}
    ]
    assert result["allRewards"]["aggregates"]["sum"]["value"] == "2.5"


def test_readonly_schema_and_request_limit(native_graphql):
    url = native_graphql["url"]
    assert (
        graphql(url, "{ __schema { mutationType { name } } }")["__schema"]["mutationType"] is None
    )
    with urlopen(url.replace("/graphql", "/graphiql"), timeout=5) as response:
        assert response.status == 200
    with pytest.raises(HTTPError) as error:
        graphql(url, "#" + "x" * 1048576 + "\n{ __typename }")
    assert error.value.code == 413
    permissions = command(
        "docker",
        "exec",
        native_graphql["database"],
        "psql",
        "-U",
        "xian",
        "-d",
        "xian",
        "-Atc",
        "SELECT has_table_privilege('xian_graphql', 'events', 'SELECT'), "
        "has_table_privilege('xian_graphql', 'events', 'INSERT')",
    )
    assert permissions == "t|f"


def is_query_literal(node):
    import ast

    return (
        isinstance(node, ast.Assign)
        and isinstance(node.value, (ast.Constant, ast.JoinedStr))
        and any(isinstance(target, ast.Name) and target.id == "query" for target in node.targets)
    )


def query_documents(path):
    """Read actual consumer query literals, including their shared field fragments."""
    import ast

    tree = ast.parse(path.read_text())
    constants = {
        node.targets[0].id: node.value.value
        for node in tree.body
        if isinstance(node, ast.Assign)
        and isinstance(node.targets[0], ast.Name)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    }
    for node in ast.walk(tree):
        if is_query_literal(node):
            yield render_query(node.value, constants)


def render_query(value, constants):
    import ast

    if isinstance(value, ast.Constant):
        return value.value
    assert isinstance(value, ast.JoinedStr), ast.dump(value)
    return "".join(
        part.value if isinstance(part, ast.Constant) else constants[part.value.id]
        for part in value.values
    )


def workspace_documents():
    paths = [
        ROOT.parent / "xian-py/src/xian_py/xian_async.py",
        ROOT.parent / "xian-mcp-server/xian_server.py",
    ]
    documents = []
    for path in paths:
        if path.exists():
            documents.extend(
                {"source": str(path), "query": query} for query in query_documents(path)
            )
    bot = ROOT.parent / "xian-tg-bot"
    for path in sorted(bot.glob("plg/*/res/*.gql")):
        documents.append({"source": str(path), "query": path.read_text()})
    docs = ROOT.parent / "xian-docs-web/api/graphql.md"
    if docs.exists():
        documents.extend(
            {"source": str(docs), "query": query}
            for query in re.findall(r"```graphql\n(.*?)```", docs.read_text(), re.DOTALL)
        )
    for path in sorted((ROOT.parent / "xian-technology-web/xian_tech/pages").glob("*.py")):
        documents.extend(
            {"source": str(path), "query": query}
            for query in re.findall(
                r'(?:TRANSFER_EVENTS_QUERY|BALANCE_QUERY|SDK_BDS_\w+_QUERY) = """(.*?)"""',
                path.read_text(),
                re.DOTALL,
            )
        )
    return documents


def test_workspace_query_documents_validate(native_graphql):
    script = """
      import {
        buildClientSchema, getIntrospectionQuery, parse, validate,
      } from 'postgraphile/graphql';
      let input = '';
      for await (const chunk of process.stdin) input += chunk;
      const documents = JSON.parse(input);
      const response = await fetch('http://127.0.0.1:5000/graphql', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({query: getIntrospectionQuery()}),
      });
      const {data} = await response.json();
      const schema = buildClientSchema(data);
      const failures = documents.flatMap(({source, query}) =>
        validate(schema, parse(query)).map(error => `${source}: ${error.message}`));
      console.log(JSON.stringify({documents: documents.length, failures}));
    """
    result = json.loads(
        command(
            "docker",
            "exec",
            "-i",
            native_graphql["server"],
            "node",
            "--input-type=module",
            "-e",
            script,
            input=json.dumps(workspace_documents()),
        )
    )
    assert result["documents"] >= 3
    assert result["failures"] == []
