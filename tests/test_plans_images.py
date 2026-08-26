from __future__ import annotations

from io import BytesIO

import pytest

from fscp_mcp import images, views

from . import factories


def test_блобы_content_опознаются_по_сигнатуре(synthetic):
    """Файлы в Content/ лежат без расширения — тип только по сигнатуре.

    Вопреки первому впечатлению это не всегда PNG: попадаются и JPEG.
    """
    found = {info.media_type: (info.width, info.height) for info in synthetic.images.values()}
    assert found == {
        "image/png": factories.PNG_SIZE,
        "image/jpeg": factories.JPEG_SIZE,
    }


def test_размеры_из_заголовка_совпадают_с_фактическими(synthetic):
    """Сверяем прочитанное из IHDR с тем, что видит полноценный декодер.

    Только PNG: синтетический JPEG состоит из одних заголовков и декодеру не
    предъявишь, а размеры кадра всё равно берутся из маркера SOF.
    """
    PIL = pytest.importorskip("PIL.Image", reason="нужна Pillow: pip install -e .[img]")
    info = synthetic.images[factories.PNG_GUID]
    with PIL.open(BytesIO(synthetic.blob(info.guid))) as picture:
        assert (info.width, info.height) == picture.size


def test_ресурсы_приложения_не_путаются_с_подложками(synthetic):
    """GKModule/Images/Zone.png — иконка внутри Global Monitor, не запись архива."""
    assert synthetic.resource_refs == {factories.APP_RESOURCE}
    reference = views.image_reference(synthetic, factories.APP_RESOURCE)
    assert reference["kind"] == "app_resource"
    assert reference["in_archive"] is False


def test_ссылка_на_подложку_разрешается(synthetic):
    reference = views.image_reference(synthetic, factories.PNG_GUID)
    assert reference["kind"] == "content"
    assert reference["in_archive"] is True
    assert reference["media_type"] == "image/png"


def test_ссылка_на_отсутствующую_подложку(synthetic):
    reference = views.image_reference(synthetic, "44444444-0000-4000-8000-999999999999")
    assert reference["kind"] == "content"
    assert reference["in_archive"] is False


def test_jpeg_размеры_читаются():
    """Минимальный JPEG: SOI + APP0 + SOF0 с кадром 640x480."""
    blob = factories.jpeg()
    assert images.sniff(blob) == "image/jpeg"
    assert images.jpeg_dimensions(blob) == factories.JPEG_SIZE
    assert images.jpeg_dimensions(images.PNG_MAGIC) is None
    assert images.dimensions(blob) == factories.JPEG_SIZE


def test_подложки_не_осиротели(synthetic):
    assert not [g for g in synthetic.images if not synthetic.image_refs.get(g)]
    assert not synthetic.missing_image_refs


def test_дерево_планов_и_вложенность(synthetic):
    tree = views.plan_tree(synthetic)
    assert len(tree) == len(synthetic.plan_roots) == 1
    assert tree[0]["name"] == factories.PLAN_NAME
    assert [child["name"] for child in tree[0]["children"]] == [factories.NESTED_PLAN_NAME]


def test_объекты_вложенного_плана_не_приписаны_родителю(synthetic):
    """Поддерево Children принадлежит вложенному плану, а не корневому."""
    root_items = [uid for uid, _ in synthetic.plan_objects_by_plan[factories.PLAN_ROOT]]
    nested_items = [uid for uid, _ in synthetic.plan_objects_by_plan[factories.PLAN_NESTED]]
    assert factories.D_AM4 in nested_items
    assert factories.D_AM4 not in root_items


def test_объекты_плана_разрешаются_в_имена(synthetic):
    detail = views.plan_detail(synthetic, factories.PLAN_ROOT)
    assert detail["objects_total"] == len(detail["objects"]) == 3
    assert detail["background"]["guid"] == factories.PNG_GUID

    by_uid = {o["item_uid"]: o for o in detail["objects"]}
    assert by_uid[factories.D_IP1]["resolved"] == factories.IP_NAME
    assert by_uid[factories.ZONE1]["resolved"] == factories.ZONE_NAMES[0]
    # Подпись кэшируется при отрисовке и после перенумерации АЛС расходится с
    # фактическим адресом — это штатный дефект, а не ошибка разбора.
    assert by_uid[factories.D_IP2]["label"] == factories.STALE_LABEL


def test_обратная_ссылка_объект_на_планах(synthetic):
    placements = views.plan_placements(synthetic, factories.D_IP1)
    assert [p["plan_uid"] for p in placements] == [factories.PLAN_ROOT]
    assert placements[0]["plan"] == factories.PLAN_NAME


def test_сниффер_распознаёт_форматы():
    assert images.sniff(images.PNG_MAGIC + b"\x00" * 8) == "image/png"
    assert images.sniff(b"\xff\xd8\xff\xe0") == "image/jpeg"
    assert images.sniff(b"<svg xmlns=") == "image/svg+xml"
    assert images.sniff(b"\x00\x01\x02\x03") == "application/octet-stream"
    assert images.png_dimensions(b"nope") is None
