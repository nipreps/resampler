# resampler
Single-shot resampling utility for nipreps derivatives

## Quick-start

Using `pipenv`:

```console
pipenv run resample --help
```

## Install

This script currently depends on unreleased features in niworkflows and sdcflows,
as well as [Typer](https://typer.tiangolo.com/).

```console
pip install git+https://github.com/nipreps/niworkflows.git \
            git+https://github.com/nipreps/sdcflows.git \
            typer
```

These should pull in all the other imports as dependencies.
