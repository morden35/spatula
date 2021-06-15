from invoke import task


@task
def docs(c):
    c.run("poetry run mkdocs serve", pty=True)


@task
def test(c, args=""):
    c.run("poetry run pytest --cov=src/ --cov-report html " + args, pty=True)


@task
def mypy(c):
    c.run("poetry run mypy src/", pty=True)


@task
def lint(c):
    c.run("poetry run flake8 src/ tests/ --ignore=E203,E501,W503", pty=True)
    c.run("poetry run black --check src/ tests/", pty=True)


@task(lint, mypy, test)
def release(c, old, new):
    c.run(
        "poetry run bump2version src/spatula/cli.py pyproject.toml docs/cli.md "
        f"--current-version {old} --new-version {new} --commit --tag --allow-dirty",
        pty=True,
    )
    c.run("git push", pty=True)
    c.run("git push --tags", pty=True)
    c.run("poetry publish --build", pty=True)
    c.run("poetry run mkdocs gh-deploy", pty=True)
