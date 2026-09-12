# Local ride data

Drop your video and matching GPX file in this folder before using the sync
studio or CLI. For example:

```text
data/
├── ride.mov
├── ride.gpx
└── ride_overlay.mp4  # created after rendering
```

Ride files and rendered videos in this folder are ignored by Git, so they are
not committed or published with the project. Only this README and `.gitkeep`
are tracked.

The CLI also accepts relative or absolute paths outside this folder. When
`--out` is omitted, the rendered file is saved beside the input video with an
`_overlay.mp4` suffix.
