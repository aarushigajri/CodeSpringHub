"""One error type for "the user asked for something they can't have or that's wrong".

It subclasses the MCP ToolError so Claude gets the message as a normal tool error,
and carries an HTTP status so the web portal can return the same message.
Messages are written for non-specialists: say what happened and what to do next.
"""

from mcp.server.mcpserver.exceptions import ToolError


class HubError(ToolError):
    status = 400


class NotFound(HubError):
    status = 404


class Forbidden(HubError):
    status = 403
