"""add docker status tool

Seeds the built-in DockerStatusTool and attaches it to the default persona (id=0),
which is the persona the Slack bot uses (ONYX_PERSONA_ID default 0).

Revision ID: f4d0c5e21a90
Revises: 99ecd56cb2ce
Create Date: 2026-06-15 19:30:00.000000

"""

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "f4d0c5e21a90"
down_revision = "99ecd56cb2ce"
branch_labels = None
depends_on = None


IN_CODE_TOOL_ID = "DockerStatusTool"
TOOL = {
    "name": "list_docker_containers",
    "display_name": "Docker Status",
    "description": (
        "List the Docker containers currently running on the host, including each "
        "container's name, image, status, and published ports."
    ),
    "in_code_tool_id": IN_CODE_TOOL_ID,
    "enabled": True,
}


def upgrade() -> None:
    conn = op.get_bind()

    existing = conn.execute(
        sa.text("SELECT id FROM tool WHERE in_code_tool_id = :in_code_tool_id"),
        {"in_code_tool_id": IN_CODE_TOOL_ID},
    ).fetchone()

    if existing:
        tool_id = existing[0]
        conn.execute(
            sa.text(
                """
                UPDATE tool
                SET name = :name,
                    display_name = :display_name,
                    description = :description,
                    enabled = :enabled
                WHERE in_code_tool_id = :in_code_tool_id
                """
            ),
            TOOL,
        )
    else:
        conn.execute(
            sa.text(
                """
                INSERT INTO tool (name, display_name, description, in_code_tool_id, enabled)
                VALUES (:name, :display_name, :description, :in_code_tool_id, :enabled)
                """
            ),
            TOOL,
        )
        tool_id = conn.execute(
            sa.text("SELECT id FROM tool WHERE in_code_tool_id = :in_code_tool_id"),
            {"in_code_tool_id": IN_CODE_TOOL_ID},
        ).fetchone()[0]

    # Attach to the default persona (id=0) if not already attached.
    conn.execute(
        sa.text(
            """
            INSERT INTO persona__tool (persona_id, tool_id)
            VALUES (0, :tool_id)
            ON CONFLICT DO NOTHING
            """
        ),
        {"tool_id": tool_id},
    )


def downgrade() -> None:
    conn = op.get_bind()

    result = conn.execute(
        sa.text("SELECT id FROM tool WHERE in_code_tool_id = :in_code_tool_id"),
        {"in_code_tool_id": IN_CODE_TOOL_ID},
    ).fetchone()

    if not result:
        return

    tool_id = result[0]
    conn.execute(
        sa.text("DELETE FROM persona__tool WHERE tool_id = :tool_id"),
        {"tool_id": tool_id},
    )
    conn.execute(
        sa.text("DELETE FROM tool WHERE id = :tool_id"),
        {"tool_id": tool_id},
    )
