## Initial Set Up

This project uses Poetry for dependency management. To get started:

1. [Install Pipx](https://github.com/pypa/pipx?tab=readme-ov-file#on-macos). It's a tool to install and run Python apps.
2. [Install Poetry using Pipx](https://python-poetry.org/docs/#installation).
3. Install [PyEnv version manager](https://github.com/pyenv/pyenv) to install the current Pythons version used by this project.
4. Install Python 3.12 using PyEnv -> `pyenv install 3.12`
5. Set the Python version for the project -> `pyenv local 3.12`
6. Create virtual env locally -> `poetry env use python3.12`
7. Run `poetry install` to install dependencies.