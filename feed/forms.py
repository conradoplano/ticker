from zoneinfo import available_timezones

from django import forms

from accounts.models import User

from .refresh import parse_time

FREQUENCIES = {
    "1": ["07:00"],
    "2": ["07:00", "17:00"],
    "4": ["07:00", "12:00", "17:00", "21:00"],
    "8": ["06:00", "08:00", "10:00", "12:00", "14:00", "17:00", "19:00", "21:00"],
}


def clean_times(text):
    times = []
    for part in str(text or "").replace(";", ",").replace(" ", ",").split(","):
        if not part.strip():
            continue
        moment = parse_time(part)
        if moment is None:
            raise forms.ValidationError(f"“{part.strip()}” is not a time like 07:00.")
        times.append(moment.strftime("%H:%M"))
    times = sorted(set(times))
    if not times:
        raise forms.ValidationError("Choose at least one time.")
    if len(times) > 24:
        raise forms.ValidationError("At most 24 times a day.")
    return times


def clean_zone(value):
    value = (value or "").strip()
    if value not in available_timezones():
        raise forms.ValidationError("Unknown time zone.")
    return value


class TimesField(forms.CharField):
    def prepare_value(self, value):
        if isinstance(value, list):
            return ", ".join(value)
        return value

    def clean(self, value):
        return clean_times(super().clean(value))


class PreferencesForm(forms.ModelForm):
    refresh_times = TimesField(
        label="Refresh times", help_text="Local times, separated by commas, e.g. 07:00, 12:00, 17:30.")
    discovery_share = forms.TypedChoiceField(
        label="New-topic discovery", choices=User.Discovery.choices, coerce=int)

    class Meta:
        model = User
        fields = ["time_zone", "refresh_times", "discovery_share"]

    def clean_time_zone(self):
        return clean_zone(self.cleaned_data["time_zone"])


class ProfileForm(forms.ModelForm):
    class Meta:
        model = User
        fields = ["profile_text", "avoid_text", "learned_profile"]
        labels = {
            "profile_text": "What you want",
            "avoid_text": "What you do not want",
            "learned_profile": "What I have learned about you",
        }
        help_texts = {
            "profile_text": "E.g. “I prefer analysis over breaking alerts.”",
            "avoid_text": "E.g. “no celebrity news, little local crime”.",
            "learned_profile": "Rewritten every week from your ratings. You can edit it.",
        }
        widgets = {
            "profile_text": forms.Textarea(attrs={"rows": 3}),
            "avoid_text": forms.Textarea(attrs={"rows": 2}),
            "learned_profile": forms.Textarea(attrs={"rows": 5}),
        }
