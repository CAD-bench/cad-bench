from bench import runner

AVAILABLE_COMMANDS = (
    runner.eval_harness_main,
    runner.render_views_main,
    runner.docs_cli,
    runner.export_task_media_main,
)


if __name__ == "__main__":
    raise SystemExit(
        "Use one of the installed commands: eval-harness, render-cad-views, "
        "build123d-docs-bundle, or export-task-media."
    )
