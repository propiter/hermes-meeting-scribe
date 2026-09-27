"""Re-analysis keeps action-item ids stable when the LLM rephrases (E2E finding).

Ids are content-derived (title + owner). In the real run a reprocess turned "Preparar el plan de
pruebas de carga" into "Preparar plan de pruebas de carga": a new id, a new idempotency key, and
a DUPLICATE Kanban task. ``reconcile_ids`` maps each new item back to the previous analysis' item.
"""
from meeting_scribe.analyze.reconcile import reconcile_ids
from meeting_scribe.domain.ids import action_item_id
from meeting_scribe.domain.models import ActionItem, ActionStatus


def item(title, owner="1", quote="", t0=None, **kw):
    return ActionItem(id=action_item_id(title, owner), title=title, owner_speaker_id=owner, quote=quote, t0=t0, **kw)


def test_rephrased_title_keeps_previous_id():
    old = [item("Preparar el plan de pruebas de carga", "2", "prepara el plan", 72.0)]
    new = [item("Preparar plan de pruebas de carga", "2", "prepara el plan", 66.0)]
    out = reconcile_ids(old, new)
    assert [a.id for a in out] == [old[0].id] and out[0].title == "Preparar plan de pruebas de carga"


def test_two_items_sharing_a_quote_map_to_their_own_previous_ids():
    q = "Yo me comprometo a escribir la nota de lanzamiento y a crear las tareas"
    old = [item("Escribir la nota de lanzamiento para clientes", quote=q, t0=86),
           item("Crear tareas en el tablero de operaciones", quote=q, t0=86)]
    new = [item("Crear tareas en el tablero de Operaciones", quote=q, t0=86),
           item("Escribir nota de lanzamiento para clientes", quote=q, t0=86)]
    assert [a.id for a in reconcile_ids(old, new)] == [old[1].id, old[0].id]


def test_different_owner_or_unrelated_title_gets_a_new_id():
    old = [item("Migrar el DNS a Cloudflare", "3")]
    new = [item("Migrar el DNS a Cloudflare", "2"), item("Comprar dominios nuevos", "3")]
    out = reconcile_ids(old, new)
    assert [a.id for a in out] == [new[0].id, new[1].id]


def test_each_previous_id_is_used_once_and_duplicates_are_dropped():
    old = [item("Enviar informe")]
    new = [item("Enviar el informe"), item("Enviar informe semanal")]
    out = reconcile_ids(old, new)
    assert out[0].id == old[0].id and out[1].id != old[0].id and len({a.id for a in out}) == 2


def test_human_status_is_not_copied_here():
    """Status lives in the repository (sync keeps it on surviving ids); reconcile only fixes ids."""
    old = [item("Enviar informe", status=ActionStatus.APPROVED)]
    out = reconcile_ids(old, [item("Enviar el informe")])
    assert out[0].id == old[0].id and out[0].status is ActionStatus.PENDING


def test_no_previous_items_is_identity():
    new = [item("Algo")]
    assert reconcile_ids([], new) == new
