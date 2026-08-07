#!/usr/bin/env python3
"""MARSOUD-COSMETICS-CATEGORY-SEED (2026-08-07) — seeds the cosmetics
product taxonomy (14 groups / ~141 categories) for one company.

Source: client-provided classification doc (skin care, sun care, hair
care, hair color, body care, deodorants, oral care, makeup, fragrances,
nail care, shaving, feminine care, baby care, men's grooming).

Visibility decision (per MARSOUD-CATEGORY-VISIBILITY-01): every group
created here is visible in pos / vendor_bills / customer_invoices, and
HIDDEN from manufacturing. That's a group-level flag, so every category
under it inherits "hidden from manufacturing" for free — no per-category
overrides needed. If a specific category later needs to appear in
Manufacturing (e.g. a raw material bought for in-house blending), flip
that one category's visible_in_manufacturing to True in the category
screen; it overrides the group default.

Idempotent: matches existing ProductGroup/ProductCategory by
(company_id, name) / (company_id, group_id, name) and skips what's
already there, so re-running after a partial apply is safe.

Usage:
    flask seed-cosmetics-categories --company-id 106                 # dry-run
    flask seed-cosmetics-categories --company-id 106 --apply         # write
"""
import click
from flask.cli import with_appcontext

from app import db
from app.models import ProductGroup, ProductCategory


# (group_name, [category_name, ...])
TAXONOMY = [
    ("العناية بالبشرة", [
        "غسول الوجه", "تونر", "مرطبات الوجه", "سيروم", "كريمات الوجه",
        "كريمات حول العين", "ماسكات الوجه", "مقشرات الوجه",
        "منتجات تفتيح البشرة", "منتجات مقاومة التجاعيد",
        "منتجات العناية بالشفاه", "منتجات البشرة الدهنية",
        "منتجات البشرة الجافة", "منتجات البشرة الحساسة",
        "منتجات حب الشباب", "مزيلات المكياج",
    ]),
    ("واقي الشمس", [
        "واقي شمس للوجه", "واقي شمس للجسم", "واقي شمس للأطفال",
        "واقي شمس للبشرة الحساسة", "واقي شمس ملون", "After Sun",
        "Self Tanning",
    ]),
    ("العناية بالشعر", [
        "شامبو", "بلسم", "ماسكات الشعر", "سيروم الشعر", "زيوت الشعر",
        "كريمات الشعر", "Leave-in Conditioner", "منتجات علاج تساقط الشعر",
        "منتجات تكثيف الشعر", "منتجات ترطيب الشعر", "منتجات الشعر الجاف",
        "منتجات الشعر الدهني", "منتجات الشعر المجعد",
        "منتجات علاج القشرة", "Styling Products",
    ]),
    ("صبغات وعلاجات الشعر", [
        "صبغات الشعر", "كريمات الصبغة", "شامبو ملون", "Hair Color Spray",
        "مزيلات لون الشعر", "تفتيح الشعر", "Bleaching Products",
        "منتجات فرد الشعر", "منتجات التجعيد", "منتجات تثبيت الشعر",
    ]),
    ("العناية بالجسم", [
        "شاور جل", "صابون الجسم", "لوشن الجسم", "كريم الجسم",
        "زيوت الجسم", "Body Scrub", "كريمات اليدين", "كريمات القدمين",
        "منتجات العناية بالأقدام", "منتجات العناية باليدين",
        "منتجات إزالة الشعر", "منتجات تفتيح الجسم",
    ]),
    ("مزيلات العرق", [
        "Deodorant Spray", "Roll-on", "Stick", "كريم مزيل العرق",
        "Antiperspirant", "مزيلات العرق للنساء", "مزيلات العرق للرجال",
    ]),
    ("العناية بالفم والأسنان", [
        "معجون الأسنان", "غسول الفم", "خيط الأسنان", "فرش الأسنان",
        "فرش الأسنان الكهربائية", "منتجات تبييض الأسنان",
        "منتجات العناية باللثة", "Breath Fresheners",
    ]),
    ("المكياج", [
        "Foundation", "Concealer", "Powder", "Blush", "Highlighter",
        "Bronzer", "Makeup Primer", "Makeup Fixer", "Mascara", "Eyeliner",
        "Eye Pencil", "Eyeshadow", "Eyebrow Pencil", "Eyebrow Gel",
        "False Eyelashes", "Lipstick", "Lip Gloss", "Lip Liner",
        "Lip Balm", "Lip Tint",
    ]),
    ("العطور", [
        "عطور نسائية", "عطور رجالية", "عطور للجنسين", "Body Mist",
        "Eau de Parfum", "Eau de Toilette", "Cologne", "معطرات الجسم",
    ]),
    ("العناية بالأظافر", [
        "Nail Polish", "Nail Polish Remover", "Nail Treatment",
        "Nail Cream", "Cuticle Care", "Base Coat", "Top Coat",
        "أدوات العناية بالأظافر",
    ]),
    ("الحلاقة", [
        "كريم الحلاقة", "Foam", "Gel", "After Shave", "Pre-Shave",
        "منتجات العناية باللحية", "Beard Oil", "Beard Balm",
    ]),
    ("العناية النسائية", [
        "منتجات النظافة الشخصية الخارجية", "Feminine Wash",
        "مزيلات الروائح الشخصية", "مناديل النظافة الشخصية",
        "منتجات العناية بالمنطقة الحساسة",
    ]),
    ("منتجات الأطفال", [
        "Baby Shampoo", "Baby Wash", "Baby Lotion", "Baby Oil",
        "Baby Cream", "Baby Powder", "Baby Wipes",
        "منتجات العناية بالشعر للأطفال", "منتجات العناية بالبشرة للأطفال",
    ]),
    ("العناية بالرجال", [
        "Beard Care", "Shaving", "Face Care for Men",
        "Hair Care for Men", "Deodorants", "Men's Perfumes",
        "After Shave", "Men's Body Care",
    ]),
]


def run(company_id, dry_run=True):
    """Create missing groups/categories for company_id. Returns a summary.

    Returns:
        groups_created, groups_existing, categories_created,
        categories_existing, plan (list of str, human-readable)
    """
    groups_created = 0
    groups_existing = 0
    categories_created = 0
    categories_existing = 0
    plan = []

    existing_groups = {
        g.name: g for g in ProductGroup.query.filter_by(
            company_id=company_id).all()
    }

    for group_name, category_names in TAXONOMY:
        group = existing_groups.get(group_name)
        if group is None:
            plan.append(f"[GROUP+ ] {group_name}")
            groups_created += 1
            if not dry_run:
                group = ProductGroup(
                    company_id=company_id,
                    name=group_name,
                    is_active=True,
                    visible_in_pos=True,
                    visible_in_manufacturing=False,   # <-- the ask
                    visible_in_vendor_bills=True,
                    visible_in_customer_invoices=True,
                )
                db.session.add(group)
                db.session.flush()  # get group.id for its categories
                existing_groups[group_name] = group
        else:
            groups_existing += 1
            # Group already exists — make sure it's hidden from
            # manufacturing too, since that's the whole point of the ask.
            if group.visible_in_manufacturing:
                plan.append(
                    f"[GROUP~ ] {group_name} (visible_in_manufacturing "
                    f"True -> False)")
                if not dry_run:
                    group.visible_in_manufacturing = False

        existing_categories = set()
        if group is not None and group.id is not None:
            existing_categories = {
                c.name for c in ProductCategory.query.filter_by(
                    company_id=company_id, group_id=group.id).all()
            }

        for cat_name in category_names:
            if cat_name in existing_categories:
                categories_existing += 1
                continue
            plan.append(f"  [CAT+  ] {group_name} / {cat_name}")
            categories_created += 1
            if not dry_run and group is not None:
                db.session.add(ProductCategory(
                    company_id=company_id,
                    group_id=group.id,
                    name=cat_name,
                    is_active=True,
                    # NULL on all four flags = inherit from the group.
                ))

    if not dry_run:
        db.session.commit()

    return {
        "groups_created": groups_created,
        "groups_existing": groups_existing,
        "categories_created": categories_created,
        "categories_existing": categories_existing,
        "plan": plan,
    }


@click.command("seed-cosmetics-categories")
@click.option("--company-id", type=int, required=True,
              help="Target company id (e.g. 106).")
@click.option("--apply", is_flag=True,
              help="Actually write the groups/categories (default: dry-run).")
@with_appcontext
def seed_cli(company_id, apply):
    """Seed the 14-group cosmetics category taxonomy for one company,
    hidden from Manufacturing, visible everywhere else."""
    result = run(company_id, dry_run=not apply)
    tag = "APPLIED" if apply else "DRY-RUN"

    click.echo(f"=== seed-cosmetics-categories [{tag}] company_id={company_id} ===")
    for line in result["plan"]:
        click.echo(line)
    click.echo("---")
    click.echo(f"groups:      {result['groups_created']} new, "
               f"{result['groups_existing']} already existed")
    click.echo(f"categories:  {result['categories_created']} new, "
               f"{result['categories_existing']} already existed")
    if not apply:
        click.echo("\nThis was a DRY-RUN. Re-run with --apply to write.")
