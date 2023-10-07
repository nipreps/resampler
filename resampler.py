from pathlib import Path

import h5py
import nibabel as nb
import nitransforms as nt
import niworkflows.data
import numpy as np
import typer
from bids import BIDSLayout
from nitransforms.io.itk import ITKCompositeH5
from scipy import ndimage as ndi
from scipy.sparse import hstack as sparse_hstack
from sdcflows.transform import grid_bspline_weights
from sdcflows.utils.tools import ensure_positive_cosines
from templateflow import api as tf
from typing_extensions import Annotated

nipreps_cfg = niworkflows.data.load('nipreps.json')


def find_bids_root(path: Path) -> Path:
    for parent in path.parents:
        if Path.exists(parent / 'dataset_description.json'):
            return parent
    raise ValueError(f'Cannot detect BIDS dataset containing {path}')


def resample_vol(
    data: np.ndarray,
    coordinates: np.ndarray,
    pe_info: tuple[int, float],
    hmc_xfm: np.ndarray | None,
    fmap_hz: np.ndarray,
    output: np.dtype | np.ndarray | None = None,
    order: int = 3,
    mode: str = 'constant',
    cval: float = 0.0,
    prefilter: bool = True,
) -> np.ndarray:
    """Resample a volume at specified coordinates

    This function implements simultaneous head-motion correction and
    susceptibility-distortion correction. It accepts coordinates in
    the source voxel space. It is the responsibility of the caller to
    transform coordinates from any other target space.

    Parameters
    ----------
    data
        The data array to resample
    coordinates
        The first-approximation voxel coordinates to sample from ``data``
        The first dimension should have length ``data.ndim``. The further
        dimensions have the shape of the target array.
    pe_info
        The readout vector in the form of (axis, signed-readout-time)
        ``(1, -0.04)`` becomes ``[0, -0.04, 0]``, which indicates that a
        +1 Hz deflection in the field shifts 0.04 voxels toward the start
        of the data array in the second dimension.
    hmc_xfm
        Affine transformation accounting for head motion from the individual
        volume into the BOLD reference space. This affine must be in VOX2VOX
        form.
    fmap_hz
        The fieldmap, sampled to the target space, in Hz
    output
        The dtype or a pre-allocated array for sampling into the target space.
        If pre-allocated, ``output.shape == coordinates.shape[1:]``.
    order
        Order of interpolation (default: 3 = cubic)
    mode
        How ``data`` is extended beyond its boundaries. See
        :func:`scipy.ndimage.map_coordinates` for more details.
    cval
        Value to fill past edges of ``data`` if ``mode`` is ``'constant'``.
    prefilter
        Determines if ``data`` is pre-filtered before interpolation.

    Returns
    -------
    resampled_array
        The resampled array, with shape ``coordinates.shape[1:]``.
    """
    if hmc_xfm is not None:
        # Move image with the head
        coords_shape = coordinates.shape
        coordinates = nb.affines.apply_affine(
            hmc_xfm, coordinates.reshape(coords_shape[0], -1).T
        ).T.reshape(coords_shape)

    vsm = fmap_hz * pe_info[1]
    coordinates[pe_info[0], ...] += vsm

    jacobian = 1 + np.gradient(vsm, axis=pe_info[0])

    result = ndi.map_coordinates(
        data,
        coordinates,
        output=output,
        order=order,
        mode=mode,
        cval=cval,
        prefilter=prefilter,
    )
    result *= jacobian
    return result


def resample_series(
    data: np.ndarray,
    coordinates: np.ndarray,
    pe_info: list[tuple[int, float]],
    hmc_xfms: list[np.ndarray] | None,
    fmap_hz: np.ndarray,
    output_dtype: np.dtype | None = None,
    order: int = 3,
    mode: str = 'constant',
    cval: float = 0.0,
    prefilter: bool = True,
) -> np.ndarray:
    """Resample a 4D time series at specified coordinates

    This function implements simultaneous head-motion correction and
    susceptibility-distortion correction. It accepts coordinates in
    the source voxel space. It is the responsibility of the caller to
    transform coordinates from any other target space.

    Parameters
    ----------
    data
        The data array to resample
    coordinates
        The first-approximation voxel coordinates to sample from ``data``.
        The first dimension should have length 3.
        The further dimensions determine the shape of the target array.
    pe_info
        A list of readout vectors in the form of (axis, signed-readout-time)
        ``(1, -0.04)`` becomes ``[0, -0.04, 0]``, which indicates that a
        +1 Hz deflection in the field shifts 0.04 voxels toward the start
        of the data array in the second dimension.
    hmc_xfm
        A sequence of affine transformations accounting for head motion from
        the individual volume into the BOLD reference space.
        These affines must be in VOX2VOX form.
    fmap_hz
        The fieldmap, sampled to the target space, in Hz
    output_dtype
        The dtype of the output array.
    order
        Order of interpolation (default: 3 = cubic)
    mode
        How ``data`` is extended beyond its boundaries. See
        :func:`scipy.ndimage.map_coordinates` for more details.
    cval
        Value to fill past edges of ``data`` if ``mode`` is ``'constant'``.
    prefilter
        Determines if ``data`` is pre-filtered before interpolation.

    Returns
    -------
    resampled_array
        The resampled array, with shape ``coordinates.shape[1:] + (N,)``,
        where N is the number of volumes in ``data``.
    """
    if data.ndim == 3:
        return resample_vol(
            data,
            coordinates,
            pe_info[0],
            hmc_xfms[0] if hmc_xfms else None,
            fmap_hz,
            output_dtype,
            order,
            mode,
            cval,
            prefilter,
        )

    out_array = np.zeros(
        coordinates.shape[1:] + data.shape[-1:], dtype=output_dtype
    )

    for volid, volume in enumerate(np.rollaxis(data, -1, 0)):
        resample_vol(
            data=volume,
            coordinates=coordinates.copy(),
            pe_info=pe_info[volid],
            hmc_xfm=hmc_xfms[volid] if hmc_xfms else None,
            fmap_hz=fmap_hz,
            output=out_array[..., volid],
            order=order,
            mode=mode,
            cval=cval,
            prefilter=prefilter,
        )

    return out_array


def parse_combined_hdf5(h5_fn, to_ras=True):
    # Borrowed from https://github.com/feilong/process
    # process.resample.parse_combined_hdf5()
    h = h5py.File(h5_fn)
    xform = ITKCompositeH5.from_h5obj(h)
    affine = xform[0].to_ras()
    # Confirm these transformations are applicable
    assert (
        h['TransformGroup']['2']['TransformType'][:][0]
        == b'DisplacementFieldTransform_float_3_3'
    )
    assert np.array_equal(
        h['TransformGroup']['2']['TransformFixedParameters'][:],
        np.array(
            [
                193.0,
                229.0,
                193.0,
                96.0,
                132.0,
                -78.0,
                1.0,
                1.0,
                1.0,
                -1.0,
                0.0,
                0.0,
                0.0,
                -1.0,
                0.0,
                0.0,
                0.0,
                1.0,
            ]
        ),
    )
    warp = h['TransformGroup']['2']['TransformParameters'][:]
    warp = warp.reshape((193, 229, 193, 3)).transpose(2, 1, 0, 3)
    warp *= np.array([-1, -1, 1])
    warp_affine = np.array(
        [
            [1.0, 0.0, 0.0, -96.0],
            [0.0, 1.0, 0.0, -132.0],
            [0.0, 0.0, 1.0, -78.0],
            [0.0, 0.0, 0.0, 1.0],
        ]
    )
    return affine, warp, warp_affine


def load_ants_h5(filename: Path) -> nt.TransformChain:
    """Load ANTs H5 files as a nitransforms TransformChain"""
    affine, warp, warp_affine = parse_combined_hdf5(filename)
    warp_transform = nt.DenseFieldTransform(nb.Nifti1Image(warp, warp_affine))
    return nt.TransformChain([warp_transform, nt.Affine(affine)])


def load_transforms(xfm_paths: list[Path]) -> nt.TransformBase:
    """Load a series of transforms as a nitransforms TransformChain

    An empty list will return an identity transform
    """
    chain = None
    for path in xfm_paths[::-1]:
        path = Path(path)
        if path.suffix == '.h5':
            xfm = load_ants_h5(path)
        else:
            xfm = nt.linear.load(path)
        if chain is None:
            chain = xfm
        else:
            chain += xfm
    if chain is None:
        chain = nt.base.TransformBase()
    return chain


def aligned(aff1: np.ndarray, aff2: np.ndarray) -> bool:
    """Determine if two affines have aligned grids"""
    return np.allclose(
        np.linalg.norm(np.cross(aff1[:-1, :-1].T, aff2[:-1, :-1].T), axis=1),
        0,
        atol=1e-3,
    )


def resample_fieldmap(
    coefficients: list[nb.Nifti1Image],
    fmap_reference: nb.Nifti1Image,
    target: nb.Nifti1Image,
    transforms: nt.TransformChain,
) -> nb.Nifti1Image:
    """Resample a fieldmap from B-Spline coefficients into a target space

    If the coefficients and target are aligned, the field is reconstructed
    directly in the target space.
    If not, then the field is reconstructed to the ``fmap_reference``
    resolution, and then resampled according to transforms.

    The former method only applies if the transform chain can be
    collapsed to a single affine transform.

    Parameters
    ----------
    coefficients
        list of B-spline coefficient files. The affine matrices are used
        to reconstruct the knot locations.
    fmap_reference
        The intermediate reference to reconstruct the fieldmap in, if
        it cannot be reconstructed directly in the target space.
    target
        The target space to to resample the fieldmap into.
    transforms
        A nitransforms TransformChain that maps images from the fieldmap
        space into the target space.

    Returns
    -------
    fieldmap
        The fieldmap encoded in ``coefficients``, resampled in the same
        space as ``target``
    """

    direct = False
    if all(isinstance(xfm, nt.Affine) for xfm in transforms):
        projected_affine = transforms.asaffine().matrix @ target.affine
        direct = aligned(projected_affine, coefficients[-1].affine)

    if direct:
        reference, _ = ensure_positive_cosines(
            target.__class__(target.dataobj, projected_affine, target.header),
        )
    else:
        if not aligned(fmap_reference.affine, coefficients[-1].affine):
            raise ValueError(
                'Reference passed is not aligned with spline grids'
            )
        reference, _ = ensure_positive_cosines(fmap_reference)

    # Generate tensor-product B-Spline weights
    colmat = sparse_hstack(
        [grid_bspline_weights(reference, level) for level in coefficients]
    ).tocsr()
    coefficients = np.hstack(
        [
            level.get_fdata(dtype='float32').reshape(-1)
            for level in coefficients
        ]
    )

    # Reconstruct the fieldmap (in Hz) from coefficients
    fmap_img = nb.Nifti1Image(
        np.reshape(colmat @ coefficients, reference.shape[:3]),
        reference.affine,
    )

    if not direct:
        fmap_img = transforms.apply(fmap_img, reference=target)

    fmap_img.header.set_intent('estimate', name='fieldmap Hz')
    fmap_img.header.set_data_dtype('float32')
    fmap_img.header['cal_max'] = max(
        (abs(fmap_img.dataobj.min()), fmap_img.dataobj.max())
    )
    fmap_img.header['cal_min'] = -fmap_img.header['cal_max']

    return fmap_img


def resample_bold(
    source: nb.Nifti1Image,
    target: nb.Nifti1Image,
    transforms: nt.TransformChain,
    fieldmap: nb.Nifti1Image | None,
    pe_info: list[tuple[int, float]] | None,
) -> nb.Nifti1Image:
    """Resample a 4D bold series into a target space, applying head-motion
    and susceptibility-distortion correction simultaneously.

    Parameters
    ----------
    source
        The 4D bold series to resample.
    target
        An image sampled in the target space.
    transforms
        A nitransforms TransformChain that maps images from the individual
        BOLD volume space into the target space.
    fieldmap
        The fieldmap, in Hz, sampled in the target space
    pe_info
        A list of readout vectors in the form of (axis, signed-readout-time)
        ``(1, -0.04)`` becomes ``[0, -0.04, 0]``, which indicates that a
        +1 Hz deflection in the field shifts 0.04 voxels toward the start
        of the data array in the second dimension.

    Returns
    -------
    resampled_bold
        The BOLD series resampled into the target space
    """
    # HMC goes last
    assert isinstance(transforms[-1], nt.linear.LinearTransformsMapping)

    # Retrieve the RAS coordinates of the target space
    coordinates = (
        nt.base.SpatialReference.factory(target).ndcoords.astype('f4').T
    )

    # We will operate in voxel space, so get the source affine
    vox2ras = source.affine
    ras2vox = np.linalg.inv(vox2ras)
    # Transform RAS2RAS head motion transforms to VOX2VOX
    hmc_xfms = [ras2vox @ xfm.matrix @ vox2ras for xfm in transforms[-1]]

    # Remove the head-motion transforms and add a mapping from boldref
    # world space to voxels. This new transform maps from world coordinates
    # in the target space to voxel coordinates in the source space.
    ref2vox = nt.TransformChain(transforms[:-1] + [nt.Affine(ras2vox)])
    mapped_coordinates = ref2vox.map(coordinates)

    # Some identities to reduce special casing downstream
    if fieldmap is None:
        fieldmap = nb.Nifti1Image(
            np.zeros(target.shape[:3], dtype='f4'), target.affine
        )
    if pe_info is None:
        pe_info = [[0, 0] for _ in range(source.shape[-1])]

    resampled_data = resample_series(
        data=source.get_fdata(dtype='f4'),
        coordinates=mapped_coordinates.T.reshape((3, *target.shape[:3])),
        pe_info=pe_info,
        hmc_xfms=hmc_xfms,
        fmap_hz=fieldmap.get_fdata(dtype='f4'),
        output_dtype='f4',
    )
    resampled_img = nb.Nifti1Image(
        resampled_data, target.affine, target.header
    )
    resampled_img.set_data_dtype('f4')

    return resampled_img


def genref(
    source_img: nb.Nifti1Image,
    target_zooms: float | tuple[float, float, float],
) -> nb.Nifti1Image:
    """Create a reference image with target voxel sizes, preserving
    the original field of view
    """
    factor = np.array(target_zooms) / source_img.header.get_zooms()[:3]
    # Generally round up to the nearest voxel, but not for slivers of voxels
    target_shape = np.ceil(np.array(source_img.shape[:3]) / factor - 0.01)
    target_affine = nb.affines.rescale_affine(
        source_img.affine, source_img.shape, target_zooms, target_shape
    )
    return nb.Nifti1Image(
        nb.fileslice.strided_scalar(target_shape.astype(int)),
        target_affine,
        source_img.header,
    )


def mkents(source, target, **entities):
    """Helper to create entity query for transforms"""
    return {'from': source, 'to': target, 'suffix': 'xfm', **entities}


def main(
    bold_file: Path,
    derivs_path: Path,
    output_dir: Path,
    space: Annotated[str, typer.Option(help='Target space to resample to')],
    resolution: Annotated[str, typer.Option(help='Target resolution')] = None,
):
    """Resample a bold file to a target space using the transforms found
    in a derivatives directory.
    """
    bids_root = find_bids_root(bold_file)
    raw = BIDSLayout(bids_root)
    derivs = BIDSLayout(derivs_path, config=[nipreps_cfg], validate=False)

    if resolution is not None:
        zooms = tuple(int(dim) for dim in resolution.split('x'))
        if len(zooms) not in (1, 3):
            raise ValueError(f'Unknown resolution: {resolution}')

    bold = raw.files[str(bold_file)]
    bold_meta = bold.get_metadata()
    entities = bold.get_entities()
    entities.pop('datatype')
    entities.pop('extension')
    entities.pop('suffix')

    bold_xfms = []
    fmap_xfms = []

    try:
        hmc = derivs.get(
            extension='.txt', **mkents('orig', 'boldref', **entities)
        )[0]
    except IndexError:
        raise ValueError('Could not find HMC transforms')

    bold_xfms.append(hmc)

    if space == 'boldref':
        reference = derivs.get(
            desc='coreg', suffix='boldref', extension='.nii.gz', **entities
        )[0]
    else:
        try:
            coreg = derivs.get(
                extension='.txt', **mkents('boldref', 'T1w', **entities)
            )[0]
        except IndexError:
            raise ValueError('Could not find coregistration transform')

        bold_xfms.append(coreg)
        fmap_xfms.append(coreg)

    if space in ('anat', 'T1w'):
        reference = derivs.get(
            subject=entities['subject'],
            desc='preproc',
            suffix='T1w',
            extension='.nii.gz',
        )[0]
        if resolution is not None:
            ref_img = genref(nb.load(reference), zooms)
    elif space not in ('anat', 'boldref', 'T1w'):
        try:
            template_reg = derivs.get(
                datatype='anat',
                extension='.h5',
                subject=entities['subject'],
                **mkents('T1w', space),
            )[0]
        except IndexError:
            raise ValueError(
                f'Could not find template registration for {space}'
            )

        bold_xfms.append(template_reg)
        fmap_xfms.append(template_reg)

        # Get mask, as shape/affine is all we need
        reference = tf.get(
            template=space,
            extension='.nii.gz',
            desc='brain',
            suffix='mask',
            resolution=resolution,
        )
        if not reference:
            # Get a hires image to resample
            reference = tf.get(
                template=space,
                extension='.nii.gz',
                desc='brain',
                suffix='mask',
                resolution='1',
            )
            ref_img = genref(nb.load(reference), zooms)

    fmapregs = derivs.get(
        extension='.txt', **mkents('boldref', derivs.get_fmapids(), **entities)
    )
    if not fmapregs:
        print('No fieldmap registrations found')
    elif len(fmapregs) > 1:
        raise ValueError(
            f'Found fieldmap registrations: {fmapregs}\nPass one as an argument.'
        )

    fieldmap = None
    if fmapregs:
        fmapreg = fmapregs[0]
        fmapid = fmapregs[0].entities['to']
        fieldmap_coeffs = derivs.get(
            fmapid=fmapid,
            desc=['coeff', 'coeff0', 'coeff1'],
            extension='.nii.gz',
        )
        fmapref = derivs.get(
            fmapid=fmapid,
            desc='preproc',
            extension='.nii.gz',
        )[0]
        transforms = load_transforms(fmap_xfms)
        # We get an inverse transform, so need to add it separately
        fmap_xfms.insert(0, fmapreg)
        transforms += ~nt.linear.load(Path(fmapreg))
        print(transforms.transforms)

        print(f'Resampling fieldmap {fmapid} into {space}:{resolution}')
        print('Coefficients:')
        print('\n'.join(f'\t{Path(c).name}' for c in fieldmap_coeffs))
        print(f'Reference: {Path(reference).name}')
        print('Transforms:')
        print('\n'.join(f'\t{Path(xfm).name}' for xfm in fmap_xfms))
        fieldmap = resample_fieldmap(
            coefficients=[nb.load(coeff) for coeff in fieldmap_coeffs],
            fmap_reference=nb.load(fmapref),
            target=ref_img,
            transforms=transforms,
        )
        fieldmap.to_filename(output_dir / f'{fmapid}.nii.gz')

    pe_dir = bold_meta['PhaseEncodingDirection']
    ro_time = bold_meta['TotalReadoutTime']
    pe_axis = 'ijk'.index(pe_dir[0])
    pe_flip = pe_dir.endswith('-')

    bold_img = nb.load(bold_file)
    source, axcodes = ensure_positive_cosines(bold_img)
    axis_flip = axcodes[pe_axis] in 'LPI'

    pe_info = (pe_axis, -ro_time if (axis_flip ^ pe_flip) else ro_time)

    if ref_img is None:
        ref_img = nb.load(reference)

    print()
    print(f'Resampling BOLD {bold_file.name} ({pe_info})')
    print(f'Reference: {Path(reference).name}')
    print('Transforms:')
    print('\n'.join(f'\t{Path(xfm).name}' for xfm in bold_xfms))
    resample_bold(
        source=source,
        target=ref_img,
        transforms=load_transforms(bold_xfms),
        fieldmap=fieldmap,
        pe_info=[pe_info for _ in range(source.shape[-1])],
    ).to_filename(output_dir / bold_file.name)


if __name__ == '__main__':
    typer.run(main)