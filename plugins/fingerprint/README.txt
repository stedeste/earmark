Fingerprint plugin for Earmark ("find similar" only)
=====================================================
Finds near-identical repeats of a sound: the same call, the same bell, the same engine
or door. It compares the shape of the spectrum over time (MFCC-style, loudness
removed) and uses no AI model, so it does not generalise: a different crow will not
match a recorded crow call, and the same call at another pitch or speed may not either.
Select examples tightly: silence or noise inside an example lowers the scores.

Nothing to install: it runs in Earmark's own Python (numpy). Reads WAV directly;
other formats need ffmpeg.

It is also the smallest complete plugin, a good starting point for writing your own
(see ../README.txt).
