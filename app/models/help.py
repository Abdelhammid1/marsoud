"""MARSOUD-HELP-CENTER-01 (Abdelhamid 2026-07-24).

CMS-managed help articles per module. Super-admin authors from
/admin/help/, users read from /help/<module_key>.
"""
import json
from datetime import datetime
from app import db


MEDIA_IMAGE = "IMAGE"
MEDIA_YOUTUBE = "YOUTUBE"
MEDIA_VIMEO = "VIMEO"
MEDIA_LINK = "LINK"


class HelpArticle(db.Model):
    __tablename__ = "help_articles"
    id = db.Column(db.Integer, primary_key=True)
    module_key = db.Column(db.String(60), nullable=False, index=True)
    title_ar = db.Column(db.String(200), nullable=False)
    title_en = db.Column(db.String(200), nullable=True)
    goal = db.Column(db.Text, nullable=True)
    general_explanation = db.Column(db.Text, nullable=True)
    tips = db.Column(db.Text, nullable=True)
    related_module_keys = db.Column(db.Text, nullable=True)
    display_order = db.Column(db.Integer, nullable=False, default=0)
    is_published = db.Column(db.Boolean, nullable=False, default=False)
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"),
                              nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow,
                           nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow,
                           onupdate=datetime.utcnow, nullable=False)

    examples = db.relationship(
        "HelpArticleExample", backref="article",
        cascade="all, delete-orphan",
        order_by="HelpArticleExample.display_order",
    )
    media = db.relationship(
        "HelpArticleMedia", backref="article",
        cascade="all, delete-orphan",
        order_by="HelpArticleMedia.display_order",
    )

    @property
    def tips_list(self):
        try:
            v = json.loads(self.tips or "[]")
            return v if isinstance(v, list) else []
        except (ValueError, TypeError):
            return []

    def set_tips(self, items):
        self.tips = json.dumps([str(x) for x in (items or []) if x])

    @property
    def related_list(self):
        try:
            v = json.loads(self.related_module_keys or "[]")
            return v if isinstance(v, list) else []
        except (ValueError, TypeError):
            return []

    def set_related(self, keys):
        self.related_module_keys = json.dumps(
            [str(k) for k in (keys or []) if k])


class HelpArticleExample(db.Model):
    __tablename__ = "help_examples"
    id = db.Column(db.Integer, primary_key=True)
    article_id = db.Column(db.Integer,
                            db.ForeignKey("help_articles.id",
                                          ondelete="CASCADE"),
                            nullable=False, index=True)
    title = db.Column(db.String(200), nullable=False)
    body = db.Column(db.Text, nullable=True)
    display_order = db.Column(db.Integer, nullable=False, default=0)


class HelpArticleMedia(db.Model):
    __tablename__ = "help_media"
    id = db.Column(db.Integer, primary_key=True)
    article_id = db.Column(db.Integer,
                            db.ForeignKey("help_articles.id",
                                          ondelete="CASCADE"),
                            nullable=False, index=True)
    type = db.Column(db.String(20), nullable=False)
    file_path = db.Column(db.String(400), nullable=True)
    url = db.Column(db.String(500), nullable=True)
    caption = db.Column(db.String(400), nullable=True)
    display_order = db.Column(db.Integer, nullable=False, default=0)

    @property
    def is_image(self):
        return self.type == MEDIA_IMAGE

    @property
    def is_video(self):
        return self.type in (MEDIA_YOUTUBE, MEDIA_VIMEO)

    def embed_url(self):
        """Return the iframe src for the video, or None."""
        if self.type == MEDIA_YOUTUBE:
            return f"https://www.youtube.com/embed/{self.url}"
        if self.type == MEDIA_VIMEO:
            return f"https://player.vimeo.com/video/{self.url}"
        return None
