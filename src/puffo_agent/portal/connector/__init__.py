"""Daemon half of the OAuth connector: claim a prepared credential and hold it.

Scope is the "电脑连接管理" box of skeleton design v0.4 §2: receive the claim
notification, fetch the credential from the server, save it locally, and report
the local connection back. Sending mail and talking to Google live elsewhere —
this package never learns which provider it is holding.
"""
