"""devis

Revision ID: f0000003quotes
Revises: f0000002layout
Create Date: 2026-11-19 17:00:00

"""
from alembic import op
import sqlalchemy as sa


revision = 'f0000003quotes'
down_revision = 'f0000002layout'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('invoice') as batch_op:
        batch_op.add_column(sa.Column('valid_until', sa.Date(), nullable=True))
        batch_op.add_column(sa.Column('decision', sa.String(length=10), nullable=True))
        batch_op.add_column(sa.Column('decided_at', sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column('quote_id', sa.Integer(), nullable=True))
        batch_op.create_index(batch_op.f('ix_invoice_quote_id'), ['quote_id'], unique=False)
        batch_op.create_foreign_key('fk_invoice_quote_id_invoice', 'invoice', ['quote_id'], ['id'], ondelete='RESTRICT')
    with op.batch_alter_table('issuer') as batch_op:
        batch_op.add_column(sa.Column('quote_prefix', sa.String(length=20), server_default='DE-', nullable=False))
        batch_op.add_column(sa.Column('quote_validity_days', sa.Integer(), server_default='30', nullable=False))


def downgrade():
    op.execute("DELETE FROM number_sequence WHERE kind = 'devis'")
    op.execute("UPDATE invoice SET quote_id = NULL")
    op.execute("DELETE FROM activity WHERE invoice_id IN (SELECT id FROM invoice WHERE kind = 'devis')")
    op.execute("DELETE FROM invoice_line WHERE invoice_id IN (SELECT id FROM invoice WHERE kind = 'devis')")
    op.execute("DELETE FROM invoice WHERE kind = 'devis'")
    with op.batch_alter_table('issuer') as batch_op:
        batch_op.drop_column('quote_validity_days')
        batch_op.drop_column('quote_prefix')
    with op.batch_alter_table('invoice') as batch_op:
        batch_op.drop_constraint('fk_invoice_quote_id_invoice', type_='foreignkey')
        batch_op.drop_index(batch_op.f('ix_invoice_quote_id'))
        for column in ('quote_id', 'decided_at', 'decision', 'valid_until'):
            batch_op.drop_column(column)
