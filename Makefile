PYTHON ?= python3

.PHONY: test worker-once worker-loop egress clean-egress env-check recent-egress dev human-browser-template

test:
	$(PYTHON) -m unittest discover -s tests -v

worker-once:
	$(PYTHON) server_worker.py once

worker-loop:
	$(PYTHON) server_worker.py loop --interval 3

egress:
	bash ./gh_egress.sh $(URL) $(METHOD) $(BODY) $(MODE)

clean-egress:
	bash ./scripts/clean-egress.sh

env-check:
	bash ./scripts/dev.sh env-check

recent-egress:
	bash ./scripts/dev.sh recent-egress

dev:
	bash ./scripts/dev.sh help

human-browser-template:
	$(PYTHON) ./scripts/render_browser_template.py --start-url "$(START_URL)" --search-term "$(SEARCH_TERM)"
