"""Classes representing various Skybrush show file formats."""

from contextlib import aclosing
from enum import IntEnum, IntFlag
from functools import partial
from io import BytesIO, SEEK_END
from math import floor
from struct import Struct
from trio import wrap_file
from typing import (
    AsyncIterable,
    Awaitable,
    Callable,
    ClassVar,
    IO,
    Iterable,
    Optional,
    Sequence,
    Union,
)

from .trajectory import TrajectorySegment, TrajectorySpecification
from .utils import crc32_mavftp as crc32, Point

__all__ = ("SkybrushBinaryShowFile",)


_SKYBRUSH_BINARY_FILE_MARKER: bytes = b"skyb"
_SKYBRUSH_BINARY_FILE_HEADER: list[bytes] = [
    # Version 0 -- never existed
    b"",
    # Version 1 header
    b"skyb\x01",
    # Version 2 header with CRC feature bit
    b"skyb\x02\x01\x00\x00\x00\x00",
]


async def _read_exactly(
    fp,
    length: int,
    offset: Optional[int] = None,
    *,
    message: str = "unexpected end of block in Skybrush file",
):
    if offset is not None:
        await fp.seek(offset)
    data = await fp.read(length)
    if len(data) != length:
        raise IOError(message)
    return data


class SkybrushBinaryFormatBlockType(IntEnum):
    """Enum representing the possible block types in a Skybrush binary file."""

    TRAJECTORY = 1
    LIGHT_PROGRAM = 2
    COMMENT = 3
    RTH_PLAN = 4
    YAW_CONTROL = 5
    EVENT_LIST = 6


class SkybrushBinaryFileBlock:
    """Class representing a single block in a Skybrush binary file."""

    def __init__(
        self,
        type: int,
        contents: Union[Optional[bytes], Callable[[], Awaitable[bytes]]],
    ):
        """Constructor.

        Parameters:
            type: type of the block
            contents: the contents of the block, or an async function that resolves
                to the contents of the block when invoked with no arguments
        """
        self.type = type

        if callable(contents):
            self._loader = contents
            self._contents = None
        else:
            self._loader = None
            self._contents = contents

    @property
    def consumed(self) -> bool:
        """Whether the block has already been consumed, i.e. loaded from the
        backing awaitable.

        Returns True if the block was constructed without an awaitable.
        """
        return self._loader is None

    async def read(self) -> bytes:
        """Reads the raw body of this block."""
        if self._contents is None and self._loader is not None:
            self._contents = await self._loader()
            self._loader = None
        return self._contents  # type: ignore


class SkybrushBinaryFileFeatures(IntFlag):
    """Feature flags used in the header of Skybrush binary show files from
    version 2 onwards.
    """

    NONE = 0
    CRC32 = 1


class SkybrushBinaryShowFile:
    """Class representing a Skybrush binary show file, backed by a
    file-like object.
    """

    _checksum_validated: bool = False
    """Whether the checksum of the file has already been validated."""

    _features: SkybrushBinaryFileFeatures = SkybrushBinaryFileFeatures.NONE
    """The optional features (checksum etc) added to this binary show file."""

    _start_of_crc_bytes: Optional[int] = None
    """Byte index of the CRC bytes in the show file, `None` if not known yet
    or if the show has no CRC bytes.
    """

    _start_of_first_block: Optional[int] = None
    """Byte index of the first block in the show file, `None` if not known yet."""

    _header_struct: ClassVar[Struct] = Struct("<BH")

    @classmethod
    def create_in_memory(cls, version: int = 2):
        return cls.from_bytes(data=None, version=version)

    @classmethod
    def from_bytes(cls, data: Optional[bytes] = None, *, version: int = 2):
        """Creates an in-memory Skybrush binary show file.

        Parameters:
            data: the show file data; `None` means to create a new show file
                with a header but no blocks yet
            version: the version number of the binary show file when it is
                created anew; ignored when `data` is not `None`
        """
        if not data:
            if version >= 1 and version < len(_SKYBRUSH_BINARY_FILE_HEADER):
                data = _SKYBRUSH_BINARY_FILE_HEADER[version]
            else:
                raise RuntimeError(f"Unsupported version number: {version}")
        return cls(BytesIO(data))

    def __init__(self, fp: IO[bytes]):
        """Constructor.

        Parameters:
            fp: the file-like object that stores the show data
        """
        if isinstance(fp, BytesIO):
            self._buffer = fp

        self._checksum_validated = False
        self._fp = wrap_file(fp)
        self._features = SkybrushBinaryFileFeatures.NONE
        self._version = None
        self._start_of_first_block = None

    async def __aenter__(self):
        await self._fp.__aenter__()
        return self

    async def __aexit__(self, exc_type, exc_value, tb):
        return await self._fp.__aexit__(exc_type, exc_value, tb)

    async def _rewind(self) -> None:
        """Rewinds the internal read/write pointer of the underlying file-like
        object to the start of the first block in the file.
        """
        if self._start_of_first_block is None:
            await self._fp.seek(0)

            self._version = await self._expect_header()
            if self._version == 1:
                self._features = SkybrushBinaryFileFeatures.NONE
            elif self._version == 2:
                feature_flags = await self._fp.read(1)
                self._features = SkybrushBinaryFileFeatures(feature_flags[0])
            else:
                raise RuntimeError("only version 1 files are supported")

            if self._features & SkybrushBinaryFileFeatures.CRC32:
                self._start_of_crc_bytes = await self._fp.tell()
                await self._fp.read(4)
            else:
                self._start_of_crc_bytes = None

            self._start_of_first_block = await self._fp.tell()
        else:
            await self._fp.seek(self._start_of_first_block)

    async def _expect_header(self) -> int:
        """Reads the beginning of the buffer to check whether the Skybrush binary
        file header is to be found there. Throws a RuntimeError if the file
        header is invalid.

        Returns:
            the Skybrush binary file schema version
        """
        header = await self._fp.read(4)
        if header != _SKYBRUSH_BINARY_FILE_MARKER:
            raise RuntimeError(f"expected Skybrush binary file header, got {header!r}")

        version = await self._fp.read(1)
        return ord(version)

    async def add_block(self, type: SkybrushBinaryFormatBlockType, body: bytes) -> None:
        """Adds a new block to the end of the Skybrush file."""
        seekable = self._fp.seekable()

        if seekable:
            await self._fp.seek(0, SEEK_END)

        if len(body) >= 65536:
            raise ValueError(
                f"body too large; maximum allowed length is 65535 bytes, got {len(body)}"
            )

        header = self._header_struct.pack(type, len(body))
        await self._fp.write(header)
        await self._fp.write(body)

    async def add_comment(
        self, comment: Union[str, bytes], encoding: str = "utf-8"
    ) -> None:
        """Adds a new comment block to the end of the Skybrush file.

        Parameters:
            comment: the comment to add
            encoding: the encoding of the comment if it is a string; ignored when
                the comment is already a bytes object
        """
        if not isinstance(comment, bytes):
            comment = comment.encode(encoding)

        return await self.add_block(SkybrushBinaryFormatBlockType.COMMENT, comment)

    async def add_encoded_light_program(self, data: bytes) -> None:
        """Adds a new light program block to the end of the Skybrush file
        with the given light program.

        Parameters:
            data: the light program, encoded in Skybrush format
        """
        return await self.add_block(SkybrushBinaryFormatBlockType.LIGHT_PROGRAM, data)

    async def add_encoded_event_list(self, data: bytes) -> None:
        """Adds a new event list (such as a pyro program) to the end of
        the Skybrush file.

        Parameters:
            data: the event list to add, encoded in Skybrush format
        """
        return await self.add_block(SkybrushBinaryFormatBlockType.EVENT_LIST, data)

    async def add_encoded_rth_plan(self, data: bytes) -> None:
        """Adds a new return-to-home plan to the end of the Skybrush file.

        Parameters:
            data: the RTH plan to add, encoded in Skybrush format
        """
        return await self.add_block(SkybrushBinaryFormatBlockType.RTH_PLAN, data)

    async def add_trajectory(self, trajectory: TrajectorySpecification) -> None:
        """Adds a new trajectory block to the end of the Skybrush file
        with the given trajectory.

        Parameters:
            trajectory: the trajectory to add
        """
        scaling_factor = trajectory.propose_scaling_factor()
        if scaling_factor >= 128:
            raise RuntimeError(
                "Trajectory covers too large an area for a Skybrush binary show file"
            )

        chunks = [bytes([scaling_factor])]  # MSB is reserved as zero
        encoder = SegmentEncoder(scaling_factor)

        # .skyb files need absolute timestamps so we need to add a constant
        # segment in front if the takeoff time is nonzero; that's why we have
        # absolute=True here
        segments = trajectory.iter_segments(max_length=65, absolute=True)
        chunks.extend(encoder.iter_encode_multiple_segments(segments))

        return await self.add_block(
            SkybrushBinaryFormatBlockType.TRAJECTORY, b"".join(chunks)
        )

    async def add_encoded_yaw_setpoints(self, data: bytes) -> None:
        """Adds a yaw control block to the end of the Skybrush file
        with the given yaw setpoints.

        Parameters:
            data: the yaw setpoint list to add, encoded in Skybrush format
        """
        return await self.add_block(SkybrushBinaryFormatBlockType.YAW_CONTROL, data)

    async def blocks(
        self, rewind: Optional[bool] = None, validate: Optional[bool] = None
    ) -> AsyncIterable[SkybrushBinaryFileBlock]:
        """Iterates over the blocks found in the file.

        Parameters:
            rewind: whether to rewind the stream to the beginning before
                iterating. `None` means to rewind if and only if the stream is
                seekable.
            validate: whether to validate the checksum of the file before
                iterating. `None` means to validate if and only if it has not
                been validated before at least once.
        """
        seekable: bool = self._fp.seekable()  # type: ignore

        if rewind is None:
            rewind = seekable

        if rewind:
            await self._rewind()

        if validate is None:
            validate = not self._checksum_validated

        if validate:
            await self.validate_checksum()

        while True:
            data = await self._fp.read(self._header_struct.size)
            if not data:
                # End of stream
                break

            block_type, length = self._header_struct.unpack(data)

            if seekable:
                offset = await self._fp.tell()
                reader = partial(_read_exactly, self._fp, length, offset=offset)
            else:
                reader = partial(_read_exactly, self._fp, length)

            block = SkybrushBinaryFileBlock(block_type, reader)
            if seekable:
                end_of_block = await self._fp.tell()
                end_of_block += length
                yield block
                await self._fp.seek(end_of_block)
            else:
                yield block
                if not block.consumed:
                    await block.read()

    @property
    def features(self) -> SkybrushBinaryFileFeatures:
        """Returns the feature flags of the file."""
        if self._version is None:
            raise RuntimeError("version header was not read yet")
        return self._features

    async def finalize(self) -> None:
        """Finalizes the file by updating its CRC block (if any)."""
        if not self._version:
            if not self._fp.seekable():
                raise RuntimeError(
                    "version number not known yet and the binary show file is not seekable"
                )

            pos = await self._fp.tell()
            try:
                await self._rewind()
            finally:
                await self._fp.seek(pos)

        await self._update_crc32()

    def get_buffer(self) -> IO[bytes]:
        """Returns the underlying buffer of the file if it is backed by an
        in-memory buffer.
        """
        if self._buffer:
            return self._buffer

        raise RuntimeError("file is not backed by an in-memory buffer")

    def get_contents(self) -> bytes:
        """Returns the contents of the underlying in-memory buffer of the file
        if it is backed by an in-memory buffer.

        Parameters:
            finalize: whether to finalize the contents before returning the
                result
        """
        if not self._buffer:
            raise RuntimeError("file is not backed by an in-memory buffer")
        return self._buffer.getvalue()

    async def read_all_blocks(
        self, rewind: Optional[bool] = None, validate: Optional[bool] = None
    ) -> list[SkybrushBinaryFileBlock]:
        """Reads and returns all the blocks found in the file.

        Parameters:
            rewind: whether to rewind the stream to the beginning before
                iterating. `None` means to rewind if and only if the stream is
                seekable.
            validate: whether to validate the checksum of the file before
                iterating. `None` means to validate if and only if it has not
                been validated before at least once.
        """
        gen = self.blocks(rewind=rewind, validate=validate)
        blocks: list[SkybrushBinaryFileBlock] = []

        async with aclosing(gen):
            async for block in gen:
                blocks.append(block)

        return blocks

    async def validate_checksum(self) -> None:
        """Validates the checksum of the file. Assumes that the file is
        seekable.

        No-op if the file header declares that the file has no checksum.

        Raises:
            RuntimeError: if the checksum of the file does not match the
                expected value
        """
        if not self.features & SkybrushBinaryFileFeatures.CRC32:
            return

        expected_crc = await self._get_expected_crc32()

        assert self._start_of_crc_bytes is not None

        position: int = await self._fp.tell()
        try:
            await self._fp.seek(self._start_of_crc_bytes)
            observed_crc: bytes = await _read_exactly(self._fp, 4)
        finally:
            await self._fp.seek(position)

        if observed_crc != expected_crc:
            expected = expected_crc.hex()
            observed = observed_crc.hex()
            raise RuntimeError(f"CRC error, expected {expected}, got {observed}")

        self._checksum_validated = True

    @property
    def version(self) -> int:
        """Returns the version number of the file."""
        if self._version is None:
            raise RuntimeError("version header was not read yet")
        return self._version

    async def _get_expected_crc32(self) -> bytes:
        """Returns the expected CRC32 checksum of the file as bytes, in little
        endian format, or all-zeros if the file header declares that the file
        needs no checksum.
        """
        if not self.features & SkybrushBinaryFileFeatures.CRC32:
            return b"\x00\x00\x00\x00"

        if not self._fp.seekable():
            raise RuntimeError("binary show file is not seekable")

        assert self._start_of_crc_bytes is not None

        position: int = await self._fp.tell()
        try:
            expected_crc = 0

            header = await _read_exactly(self._fp, self._start_of_crc_bytes, offset=0)
            expected_crc = crc32(header, expected_crc)

            await _read_exactly(self._fp, 4)  # skip old CRC, assume zeros
            expected_crc = crc32(b"\x00\x00\x00\x00", expected_crc)

            while True:
                block = await self._fp.read(4096)
                if block:
                    expected_crc = crc32(block, expected_crc)
                if len(block) < 4096:
                    break
        finally:
            await self._fp.seek(position)

        return expected_crc.to_bytes(4, "little", signed=False)

    async def _update_crc32(self) -> None:
        """Updates the CRC32 checksum of the file if it has one."""
        if not self.features & SkybrushBinaryFileFeatures.CRC32:
            return

        expected_crc = await self._get_expected_crc32()

        assert self._start_of_crc_bytes is not None

        position: int = await self._fp.tell()
        try:
            await self._fp.seek(self._start_of_crc_bytes)
            await self._fp.write(expected_crc)
        finally:
            await self._fp.seek(position)


class SegmentEncoder:
    """Encoder class for trajectory segments in the Skybrush binary show file
    format.
    """

    _point_struct: ClassVar[Struct] = Struct("<hhhh")
    _header_struct: ClassVar[Struct] = Struct("<BH")

    _scale: float

    def __init__(self, scale: float = 1):
        """Constructor.

        Parameters:
            scale: the scaling factor of the trajectory block; the real
                coordinates are multiplied by 1000 and then divided by this
                factor before rounding them to an integer that is then stored
                in the file. The scaling factor does not apply to the yaw;
                yaw angles are always encoded in 1/10th of degrees.
        """
        self._scale = 1000 / scale

    def encode_point(self, point: Point, yaw: float = 0.0) -> bytes:
        """Encodes the X, Y and Z coordinates of a point, followed by the given
        yaw coordinate.

        Args:
            point: the point to encode
            yaw: an optional yaw value to encode. Currently ignored; we have
                migrated to using separate yaw control blocks.

        Returns:
            the encoded representation of the point
        """
        x, y, z = self._scale_point(point)
        yaw = self._scale_yaw(yaw)
        return self._point_struct.pack(x, y, z, yaw)

    def encode_segment(self, segment: TrajectorySegment) -> bytes:
        """Encodes the control points and the end point of the given segment.

        Note that the start point of the segment is assumed to be identical to
        the end point of the previous segment, therefore the start point will
        not be encoded.

        Args:
            segment: the segment to encode

        Returns:
            the encoded representation of the control points and the end point
            of the segment
        """
        if not segment.has_control_points:
            # This is easier
            pass

        duration = floor(segment.duration * 1000)
        if duration < 0 or duration > 65535:
            raise RuntimeError(
                f"trajectory segment must be in the range 0-65535 msec, got {duration} msec"
            )

        xs, ys, zs = zip(*(self._scale_point(point) for point in segment.points))
        x_format, xs = self._encode_coordinate_series(xs)
        y_format, ys = self._encode_coordinate_series(ys)
        z_format, zs = self._encode_coordinate_series(zs)

        header = self._header_struct.pack(
            x_format | (y_format << 2) | (z_format << 4), duration
        )

        parts = [header]
        parts.extend(xs)
        parts.extend(ys)
        parts.extend(zs)

        return b"".join(parts)

    def encode_multiple_segments(self, segments: Iterable[TrajectorySegment]) -> bytes:
        """Encodes the start point, the control points and the end point of multiple
        segments that constitute a continuous curve.

        It is assumed that the start point of each segment is identical to the
        end point of the previous segment, therefore we will only encode the
        start point of the first segment.

        Args:
            segments: the segments to encode

        Returns:
            the encoded representation of the segments
        """
        return b"".join(self.iter_encode_multiple_segments(segments))

    def iter_encode_multiple_segments(
        self,
        segments: Iterable[TrajectorySegment],
    ) -> Iterable[bytes]:
        """Iteratively encodes an iterable of trajectory segments that are
        assumed to constitute a continuous curve.

        It is assumed that the start point of each segment is identical to the
        end point of the previous segment, therefore we will only encode the
        start point of the first segment.

        Args:
            segments: the segments to encode

        Yields:
            the representation of the first point of the first segment, followed by
            the representation of each segment without its first point. (Note that
            the last point of each segment is the same as the first point of the
            next segment so the encoding does not lose information).
        """
        first = True
        for segment in segments:
            if first:
                # Encode the start point of the trajectory
                yield self.encode_point(segment.start)
                first = False

            # Encode the segment without its start point
            yield self.encode_segment(segment)

    def _encode_coordinate_series(self, xs: Sequence[int]) -> tuple[int, list[bytes]]:
        first, *xs = xs
        if all(x == first for x in xs):
            # segment is constant, this is easy
            return 0, [b""]

        if len(xs) == 2:
            # segment is a quadratic Bezier curve, we need to promote it to
            # cubic first
            xs_float = ((first + 2 * xs[0]) / 3, (2 * xs[0] + xs[1]) / 3, xs[1])
            xs = [int(round(x)) for x in xs_float]

        coords = [x.to_bytes(2, byteorder="little", signed=True) for x in xs]
        if len(xs) == 1:
            # segment is linear
            return 1, coords

        if len(xs) == 3:
            # segment is a cubic Bezier curve
            return 2, coords

        if len(xs) == 7:
            # segment is a 7D polynomial curve
            return 3, coords

        # TODO(ntamas): convert 4-5-6D curves to 7D ones
        raise NotImplementedError(f"{len(xs)}D curves not implemented yet")

    def _scale_point(self, point: Point) -> tuple[int, int, int]:
        # We always need to round with int() here, we cannot use round(). The
        # reason is that the scaling factor was determined in a way that it is
        # guaranteed that we fit into 2 bytes with the values rounded with
        # int() but it is not guaranteed if we round with round(). Example:
        # an extremum of 131070 yields a scaling factor of 4, so
        # self._scale = 0.25. In this case, round(131070 * self._scale) = 32768,
        # which does not fit.
        return (
            int(point[0] * self._scale),
            int(point[1] * self._scale),
            int(point[2] * self._scale),
        )

    def _scale_yaw(self, yaw: float) -> int:
        # We can safely use round() here as this part won't suffer from the same
        # problems as the one outlined in _scale_points()
        yaw = round((yaw % 360) * 10)
        return yaw - 3600 if yaw >= 3600 else yaw
