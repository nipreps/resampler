**Notice: This is a legacy proof-of-concept. It is no longer maintained and there may be undisclosed bugs. As of July 6, 2026, the canonical implementation of the resampler is in [`fmriprep.interfaces.resampling`](https://github.com/nipreps/fmriprep/blob/eb167b9ffb4bf2b90bf86457748e921887ef64a1/fmriprep/interfaces/resampling.py)**

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
Once installed, run with:

```console
python resampler.py --help
```
