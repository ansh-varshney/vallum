"""The Vallum control room: the status site on its own Cloud Run service and address (vallum-status),
https://vallum-status-256944994567.us-central1.run.app/ (the app's /status forwards there).

It is Vallum's own instrument, not a feature of the link shortener: it reads only public documents of the state
store (vallum_state/*, written by code in main pipelines) and never the secrets."""
