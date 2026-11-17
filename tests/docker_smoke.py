"""Opt-in Docker integration check: python3 tests/docker_smoke.py [image]."""

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid
from contextlib import closing
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def docker(*args):
    return subprocess.check_output(["docker", *args], text=True).strip()


def snapshot(directory):
    return {
        str(path.relative_to(directory)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in directory.rglob("*")
        if path.is_file()
    }


def database_dump(path):
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as connection:
        return list(connection.iterdump())


def check_image(image, directory):
    mount = f"type=bind,src={directory},dst=/app/data"
    owner = f"{os.getuid()}:{os.getgid()}"
    docker(
        "run",
        "--rm",
        "--network",
        "none",
        image,
        "python",
        "-c",
        "import os; from pathlib import Path; "
        "assert os.getuid() != 0; "
        "assert 'OPENAI_API_KEY' not in os.environ; "
        "assert not any(Path('/app/data').iterdir()); "
        "assert all(not Path(p).exists() for p in "
        "('/app/.env', '/app/.venv', '/app/.git', '/app/tests'))",
    )

    def manage(command):
        print(
            docker(
                "run",
                "--rm",
                "--network",
                "none",
                "--user",
                owner,
                "--mount",
                mount,
                image,
                "python",
                "manage.py",
                command,
            )
        )

    manage("generate")
    first_generation = snapshot(directory)
    manage("generate")
    assert snapshot(directory) == first_generation, "Generation is not reproducible"
    manage("clean")
    database = directory / "processed" / "pedidos.sqlite3"
    first_load = database_dump(database)
    manage("clean")
    assert database_dump(database) == first_load, "Loading is not idempotent"
    before_api = snapshot(directory)

    name = f"orders-smoke-{uuid.uuid4().hex[:12]}"
    try:
        docker(
            "run",
            "--detach",
            "--name",
            name,
            "--user",
            owner,
            "--mount",
            mount + ",readonly",
            "--publish",
            "127.0.0.1::8000",
            image,
        )
        address = docker("port", name, "8000/tcp")
        base_url = f"http://{address}"
        deadline = time.monotonic() + 30
        while True:
            try:
                with urlopen(base_url + "/openapi.json", timeout=1) as response:
                    assert response.status == 200
                break
            except (URLError, TimeoutError, ConnectionError):
                if time.monotonic() >= deadline:
                    raise RuntimeError("Container API did not become ready") from None
                time.sleep(0.2)

        for question, status, code in [
            ("   ", 422, "invalid_input"),
            ("Ventas de España en marzo de 2025", 503, "service_not_ready"),
        ]:
            request = Request(
                base_url + "/query",
                data=json.dumps({"question": question}).encode(),
                headers={"Content-Type": "application/json"},
            )
            try:
                response = urlopen(request, timeout=5)
            except HTTPError as error:
                response = error
            with response:
                assert response.status == status
                assert json.load(response)["code"] == code

        # Exercise the real executor and the bind mount, without contacting an LLM.
        docker(
            "exec",
            name,
            "python",
            "-c",
            "from pathlib import Path; from query_executor import execute_query; "
            'result = execute_query("SELECT SUM(importe_total_centavos) '
            "FROM ventas_por_mes_pais WHERE país='España' AND anio_mes='2025-03'\", "
            "Path('/app/data/processed/pedidos.sqlite3')); "
            "assert result.rows == [[98900]] and not result.truncated",
        )
        docker(
            "exec",
            name,
            "python",
            "-c",
            "import errno\nfrom pathlib import Path\n"
            "try:\n    Path('/app/data/write-probe').write_text('probe')\n"
            "except OSError as error:\n    assert error.errno == errno.EROFS\n"
            "else:\n    raise AssertionError('Data mount is writable')\n",
        )
    finally:
        subprocess.run(["docker", "rm", "--force", name], check=False, capture_output=True)
    assert snapshot(directory) == before_api, "API startup or execution changed persisted data"
    print("Docker OK: reproducible CSV, idempotent load, HTTP API, read-only data, no credentials.")


if __name__ == "__main__":
    image = sys.argv[1] if len(sys.argv) > 1 else "ventas-demo"
    with tempfile.TemporaryDirectory(prefix="orders-docker-") as temporary:
        check_image(image, Path(temporary).resolve())
