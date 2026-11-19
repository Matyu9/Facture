"""destinataire ponctuel et mise en page par émetteur

Revision ID: f0000002layout
Revises: f0000001initial
Create Date: 2026-11-19 15:00:00

"""
from alembic import op
import sqlalchemy as sa


revision = 'f0000002layout'
down_revision = 'f0000001initial'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('invoice') as batch_op:
        batch_op.alter_column('customer_id', existing_type=sa.Integer(), nullable=True)
    with op.batch_alter_table('issuer') as batch_op:
        batch_op.add_column(sa.Column('pdf_layout', sa.String(length=20), server_default='classique', nullable=False))
        batch_op.add_column(sa.Column('pdf_color', sa.String(length=7), nullable=True))
        batch_op.add_column(sa.Column('logo_size', sa.String(length=10), server_default='moyen', nullable=False))
        batch_op.add_column(sa.Column('logo_position', sa.String(length=10), server_default='gauche', nullable=False))
        batch_op.add_column(sa.Column('intro_text', sa.Text(), nullable=True))
        batch_op.add_column(sa.Column('closing_text', sa.Text(), nullable=True))
        batch_op.add_column(sa.Column('recipient_label', sa.String(length=40), nullable=True))
        batch_op.add_column(sa.Column('designation_label', sa.String(length=40), nullable=True))


def downgrade():
    with op.batch_alter_table('issuer') as batch_op:
        for column in ('designation_label', 'recipient_label', 'closing_text', 'intro_text', 'logo_position',
                       'logo_size', 'pdf_color', 'pdf_layout'):
            batch_op.drop_column(column)
    # Un destinataire ponctuel n'a pas de fiche client : revenir en arrière n'est possible que s'il n'y en a aucun.
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM invoice WHERE customer_id IS NULL) THEN "
        "RAISE EXCEPTION 'Des pièces ont un destinataire ponctuel : enregistrez-les comme clients avant de revenir "
        "en arrière.'; END IF; END $$"
    )
    with op.batch_alter_table('invoice') as batch_op:
        batch_op.alter_column('customer_id', existing_type=sa.Integer(), nullable=False)
