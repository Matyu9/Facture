"""séries de numérotation des factures

Revision ID: f0000005series
Revises: f0000004numbering
Create Date: 2026-11-19 21:00:00

"""
from alembic import op
import sqlalchemy as sa


revision = 'f0000005series'
down_revision = 'f0000004numbering'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'number_series',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('issuer_id', sa.Integer(), nullable=False),
        sa.Column('code', sa.String(length=30), nullable=False),
        sa.Column('name', sa.String(length=80), nullable=False),
        sa.Column('invoice_format', sa.String(length=40), nullable=False),
        sa.Column('credit_format', sa.String(length=40), nullable=False),
        sa.Column('is_default', sa.Boolean(), server_default='0', nullable=False),
        sa.Column('archived_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['issuer_id'], ['issuer.id'], ondelete='RESTRICT'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('issuer_id', 'code', name='uq_number_series_issuer_code'),
    )
    with op.batch_alter_table('number_series') as batch_op:
        batch_op.create_index(batch_op.f('ix_number_series_issuer_id'), ['issuer_id'], unique=False)
        batch_op.create_index(
            'uq_number_series_default', ['issuer_id'], unique=True, postgresql_where=sa.text('is_default')
        )
    # Les formats de l'émetteur deviennent sa série « Générale », par défaut.
    op.execute(
        "INSERT INTO number_series (issuer_id, code, name, invoice_format, credit_format, is_default) "
        "SELECT id, 'generale', 'Générale', invoice_format, credit_format, true FROM issuer ORDER BY id"
    )

    with op.batch_alter_table('invoice') as batch_op:
        batch_op.add_column(sa.Column('series_id', sa.Integer(), nullable=True))
        batch_op.create_index(batch_op.f('ix_invoice_series_id'), ['series_id'], unique=False)
        batch_op.create_foreign_key(
            'fk_invoice_series_id_number_series', 'number_series', ['series_id'], ['id'], ondelete='RESTRICT'
        )
    op.execute(
        "UPDATE invoice SET series_id = s.id FROM number_series AS s "
        "WHERE s.issuer_id = invoice.issuer_id AND invoice.kind IN ('facture', 'avoir')"
    )

    op.add_column('number_sequence', sa.Column('series_id', sa.Integer(), server_default='0', nullable=False))
    op.execute(
        "UPDATE number_sequence SET series_id = s.id FROM number_series AS s "
        "WHERE s.issuer_id = number_sequence.issuer_id AND number_sequence.kind IN ('facture', 'avoir')"
    )
    op.drop_constraint('number_sequence_pkey', 'number_sequence', type_='primary')
    op.create_primary_key('number_sequence_pkey', 'number_sequence', ['issuer_id', 'kind', 'series_id', 'period'])

    with op.batch_alter_table('issuer') as batch_op:
        batch_op.drop_column('invoice_format')
        batch_op.drop_column('credit_format')


def downgrade():
    with op.batch_alter_table('issuer') as batch_op:
        batch_op.add_column(sa.Column('invoice_format', sa.String(length=40), nullable=False, server_default='FA-{AAAA}-{NNNN}'))
        batch_op.add_column(sa.Column('credit_format', sa.String(length=40), nullable=False, server_default='AV-{AAAA}-{NNNN}'))
    op.execute(
        "UPDATE issuer SET invoice_format = s.invoice_format, credit_format = s.credit_format "
        "FROM number_series AS s WHERE s.issuer_id = issuer.id AND s.is_default"
    )
    # Les autres séries n'ont pas d'équivalent : leurs compteurs sont perdus, leurs pièces gardent leur numéro.
    op.execute(
        "DELETE FROM number_sequence WHERE series_id <> 0 AND series_id NOT IN "
        "(SELECT id FROM number_series WHERE is_default)"
    )
    op.drop_constraint('number_sequence_pkey', 'number_sequence', type_='primary')
    op.drop_column('number_sequence', 'series_id')
    op.create_primary_key('number_sequence_pkey', 'number_sequence', ['issuer_id', 'kind', 'period'])
    with op.batch_alter_table('invoice') as batch_op:
        batch_op.drop_constraint('fk_invoice_series_id_number_series', type_='foreignkey')
        batch_op.drop_index(batch_op.f('ix_invoice_series_id'))
        batch_op.drop_column('series_id')
    op.drop_table('number_series')
