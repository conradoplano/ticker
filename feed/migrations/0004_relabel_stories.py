"""Clears the story labels given before labels had to name one specific story and before the AI saw an
example headline for each; the next refresh labels these items again (a cheap headlines-only call)."""
from django.db import migrations


def clear_labels(apps, schema_editor):
    apps.get_model("feed", "ArticleScore").objects.exclude(story_key="").update(story_key="")


class Migration(migrations.Migration):
    dependencies = [("feed", "0003_story_key")]
    operations = [migrations.RunPython(clear_labels, migrations.RunPython.noop)]
