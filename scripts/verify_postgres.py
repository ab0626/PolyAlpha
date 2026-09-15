"""Optional integration verification against a disposable local PostgreSQL container."""

import os
import secrets
import subprocess
import time
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import psycopg

from polyalpha.postgres import PostgresStore


def main():
    name = "polyalpha-verify-" + uuid4().hex[:10]
    password = secrets.token_urlsafe(24)
    env = dict(os.environ, POSTGRES_PASSWORD=password)
    container = None
    try:
        container = subprocess.check_output(
            [
                "docker",
                "run",
                "--rm",
                "-d",
                "--name",
                name,
                "-e",
                "POSTGRES_PASSWORD",
                "-e",
                "POSTGRES_USER=researcher",
                "-e",
                "POSTGRES_DB=polyalpha",
                "-p",
                "127.0.0.1::5432",
                "postgres:17",
            ],
            env=env,
            text=True,
        ).strip()
        deadline = time.monotonic() + 30
        while True:
            mapping = subprocess.check_output(
                ["docker", "port", container, "5432/tcp"], text=True
            ).strip()
            if mapping:
                break
            if time.monotonic() > deadline:
                raise RuntimeError("Docker did not publish the disposable database port")
            time.sleep(0.5)
        port = int(mapping.rsplit(":", 1)[1])
        dsn = f"host=127.0.0.1 port={port} dbname=polyalpha user=researcher password={password}"
        deadline = time.monotonic() + 30
        while True:
            try:
                store = PostgresStore(dsn)
                break
            except psycopg.OperationalError:
                if time.monotonic() > deadline:
                    raise RuntimeError("disposable PostgreSQL did not become ready") from None
                time.sleep(0.5)
        at = datetime(2026, 1, 1, tzinfo=UTC)
        with store:
            store.initialize()
            store.initialize()
            store.append("book", "token", at, {"version": 1}, at)
            assert store.latest("book", "token", at).payload == {"version": 1}
            store.append("book", "token", at + timedelta(seconds=1), {"version": 2}, at)
            assert len(list(store.replay(at))) == 1
            try:
                store.connection.execute("DELETE FROM receipts")
            except psycopg.errors.RaiseException:
                pass
            else:
                raise AssertionError("append-only trigger did not fire")
        with PostgresStore(dsn) as reopened:
            assert len(list(reopened.replay(at + timedelta(seconds=2)))) == 2
        print(
            "PostgreSQL verified: schema idempotency, point-in-time reads, "
            "durable writes after reads, append-only trigger."
        )
    finally:
        if container:
            subprocess.run(["docker", "stop", container], check=True, stdout=subprocess.DEVNULL)


if __name__ == "__main__":
    main()
