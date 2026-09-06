## Decision

Represent identity capture as a context-managed descriptor lifetime around the
existing synchronous commit callback. Capture with O_PATH and O_NOFOLLOW so
symlinks and directories remain observable without following or opening them
for content. Keep the descriptor until verification and any permitted rollback
return; close on rename/verification exceptions too. An unavailable witness
keeps today's unverifiable result, never guessed rollback.

A replacement before capture is the witnessed inode and can be rolled back
when it is not regular. A replacement after capture is a different inode and
must not be rolled back as though it were the witnessed source. The held fd
prevents the original inode number being recycled during this interval.
This does not prevent an in-place edit or make rename an atomic byte compare.
