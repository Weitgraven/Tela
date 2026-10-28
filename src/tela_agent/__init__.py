"""tela-agent: the privileged half of Tela.

The web panel runs as an unprivileged user and never touches the system
directly. Anything that needs root (writing web server config, creating
mailboxes, issuing certificates, ...) is requested from this agent over a
Unix socket. The agent only accepts a fixed set of named actions, validates
every parameter, and only talks to callers whose peer UID is allowed.
"""
