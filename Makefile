PYTHON ?= python3

.PHONY: test worker-once worker-loop egress clean-egress

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
