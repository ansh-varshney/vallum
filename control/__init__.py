"""The Vallum control room: the status site on its own Cloud Run service and address (vallum-status).

It is Vallum's own instrument, not a feature of the link shortener: it reads only public documents of the state
store (vallum_state/*, written by code in main pipelines) and never the secrets. The app's /status forwards here."""
