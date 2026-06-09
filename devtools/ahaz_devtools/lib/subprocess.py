import subprocess
from logging import DEBUG, Logger
from typing import Optional


def execute_into_logger(
    command: list[str], logger: Logger, log_level=DEBUG, input: Optional[str] = None, check: bool = True
):
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        stdin=subprocess.PIPE if input is not None else None,
    )

    assert process.stdout is not None

    # if text input was provided, write it to the process stdin and close so the process can read EOF
    if input is not None:
        assert process.stdin is not None
        process.stdin.write(input)
        process.stdin.close()

    while True:
        output = process.stdout.readline()
        if output == "" and process.poll() is not None:
            break
        if output:
            logger.log(log_level, output.strip(), stacklevel=2)

    # Simulate the same behaviour as subprocess.run()'s check=True
    if process.returncode != 0 and check:
        logger.error(f"Command '{' '.join(command)}' failed with return code {process.returncode}")
        raise subprocess.CalledProcessError(process.returncode, command)

    return process.poll()
