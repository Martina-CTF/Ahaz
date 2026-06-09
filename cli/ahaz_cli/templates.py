from pathlib import Path

from .lib.task import deserialise_task, serialise_task


def write_task_yaml(file_path: Path, task_name: str) -> None:
    template_path = Path(__file__).parent / "assets" / "task.yaml"
    task = deserialise_task(template_path.read_text())

    task.name = task_name

    task_dest = file_path / "task.yaml"
    task_dest.write_text(serialise_task(task))


def copy_example_images(dest_path: Path) -> None:
    example_image_src = Path(__file__).parent / "assets" / "hello-world"
    example_image_dest = dest_path / "hello-world"
    example_image_dest.mkdir(exist_ok=True)

    for item in example_image_src.iterdir():
        if item.is_file():
            dest_file = example_image_dest / item.name
            dest_file.write_text(item.read_text())
