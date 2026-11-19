"""numérotation réglable

Revision ID: f0000004numbering
Revises: f0000003quotes
Create Date: 2026-11-19 19:00:00

"""
from alembic import op
import sqlalchemy as sa


revision = 'f0000004numbering'
down_revision = 'f0000003quotes'
branch_labels = None
depends_on = None

FORMATS = (('invoice', 'FA-'), ('credit', 'AV-'), ('quote', 'DE-'))


def upgrade():
    with op.batch_alter_table('issuer') as batch_op:
        for name, prefix in FORMATS:
            batch_op.add_column(sa.Column(
                f'{name}_format', sa.String(length=40), nullable=False, server_default=prefix + '{AAAA}-{NNNN}'
            ))
    # Le format d'avant : <préfixe><année>-<numéro sur N chiffres>.
    for name, _ in FORMATS:
        op.execute(
            f"UPDATE issuer SET {name}_format = {name}_prefix || '{{AAAA}}-{{' || repeat('N', number_digits) || '}}'"
        )
    # Les numéros sont uniques dans toute l'appli : un format déjà pris par un autre émetteur (le « DE- » des devis
    # était le même pour tous) reçoit le code de l'émetteur en tête.
    for name, _ in FORMATS:
        op.execute(
            f"UPDATE issuer AS i SET {name}_format = left(upper(i.code), 20) || '-' || i.{name}_format "
            f"WHERE EXISTS (SELECT 1 FROM issuer AS o WHERE o.id < i.id AND o.{name}_format = i.{name}_format)"
        )
    with op.batch_alter_table('issuer') as batch_op:
        for name, _ in FORMATS:
            batch_op.drop_column(f'{name}_prefix')
        batch_op.drop_column('number_digits')

    op.add_column('number_sequence', sa.Column('period', sa.String(length=7), nullable=True))
    op.execute("UPDATE number_sequence SET period = year::text")
    op.drop_constraint('number_sequence_pkey', 'number_sequence', type_='primary')
    op.drop_column('number_sequence', 'year')
    op.alter_column('number_sequence', 'period', nullable=False)
    op.create_primary_key('number_sequence_pkey', 'number_sequence', ['issuer_id', 'kind', 'period'])


def downgrade():
    # Les séries mensuelles ou sans remise à zéro n'ont pas d'équivalent : elles sont perdues.
    op.execute("DELETE FROM number_sequence WHERE period !~ '^[0-9]{4}$'")
    op.add_column('number_sequence', sa.Column('year', sa.Integer(), nullable=True))
    op.execute("UPDATE number_sequence SET year = period::integer")
    op.drop_constraint('number_sequence_pkey', 'number_sequence', type_='primary')
    op.drop_column('number_sequence', 'period')
    op.alter_column('number_sequence', 'year', nullable=False)
    op.create_primary_key('number_sequence_pkey', 'number_sequence', ['issuer_id', 'kind', 'year'])

    with op.batch_alter_table('issuer') as batch_op:
        for name, prefix in FORMATS:
            batch_op.add_column(sa.Column(f'{name}_prefix', sa.String(length=20), nullable=False, server_default=prefix))
        batch_op.add_column(sa.Column('number_digits', sa.Integer(), nullable=False, server_default='4'))
    # Préfixe : le texte avant la première balise.
    for name, _ in FORMATS:
        op.execute(
            f"UPDATE issuer SET {name}_prefix = left(split_part({name}_format, '{{', 1), 20) "
            f"WHERE split_part({name}_format, '{{', 1) <> ''"
        )
    with op.batch_alter_table('issuer') as batch_op:
        for name, _ in FORMATS:
            batch_op.drop_column(f'{name}_format')
