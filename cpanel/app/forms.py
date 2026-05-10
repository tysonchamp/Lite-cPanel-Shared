from flask_wtf import FlaskForm

class EmptyForm(FlaskForm):
    # A generic form just for handling CSRF tokens on POST requests
    # that don't need explicit WTForms validation
    pass
