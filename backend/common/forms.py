class RenderedValuesForm:
    """
    an admin form whose changed_data holds only what the admin edited

    every field posts the value it was rendered with next to the edited one, so a form opened
    before another change never writes back a value it only displayed. disabled fields are
    left out: they never change, and the password hash must not reach the page
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            if not field.disabled:
                field.show_hidden_initial = True

    def save_edited_m2m(self) -> None:
        for field in self._meta.model._meta.many_to_many:
            if field.name in self.changed_data and field.name in self.cleaned_data:
                field.save_form_data(self.instance, self.cleaned_data[field.name])
