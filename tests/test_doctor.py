import unittest

from dockerdoctor.core import (
    lint,
    main_stage,
    parse_dockerfile,
    severity_at_least,
)

BAD = "examples/bad.Dockerfile"
GOOD = "examples/good.Dockerfile"


def codes(text):
    return [f.code for f in lint(parse_dockerfile(text))]


def read(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


class ParsingTests(unittest.TestCase):
    def test_line_continuations_become_one_instruction(self):
        stages = parse_dockerfile("FROM a:1\nRUN one \\\n  two \\\n  three\n")
        run = stages[0].named("RUN")[0]
        self.assertEqual(run.shell(), "one two three")
        self.assertEqual(run.line, 2)

    def test_comment_inside_continuation_is_dropped(self):
        stages = parse_dockerfile("FROM a:1\nRUN one \\\n# note\n  two\n")
        self.assertEqual(stages[0].named("RUN")[0].shell(), "one two")

    def test_inline_hash_is_not_a_comment(self):
        stages = parse_dockerfile("FROM a:1\nRUN echo a#b\n")
        self.assertIn("a#b", stages[0].named("RUN")[0].shell())

    def test_heredoc_body_is_not_parsed_as_instructions(self):
        text = "FROM a:1\nRUN <<EOF\napt-get install -y gcc\nEOF\nUSER app\n"
        stages = parse_dockerfile(text)
        self.assertEqual([i.keyword for i in stages[0].instructions],
                         ["RUN", "USER"])
        self.assertIn("apt-get install", stages[0].named("RUN")[0].shell())

    def test_exec_form_is_joined_for_matching(self):
        stages = parse_dockerfile('FROM a:1\nRUN ["apt-get", "install", "-y", "x"]\n')
        self.assertIn("apt-get install", stages[0].named("RUN")[0].shell())

    def test_stages_and_aliases(self):
        stages = parse_dockerfile(
            "FROM python:3.12 AS builder\nRUN x\nFROM python:3.12\nRUN y\n")
        self.assertEqual([s.alias for s in stages], ["builder", None])
        self.assertEqual(main_stage(stages).index, 1)

    def test_line_map_after_multiline_instruction(self):
        stages = parse_dockerfile("FROM a:1\nRUN a \\\n b \\\n c\nUSER app\n")
        self.assertEqual(stages[0].named("USER")[0].line, 5)


class RuleTests(unittest.TestCase):
    def test_runs_as_root_is_an_error(self):
        findings = lint(parse_dockerfile("FROM a:1\n"))
        root = [f for f in findings if f.code == "runs-as-root"]
        self.assertEqual(root[0].severity, "error")

    def test_non_root_user_silences_the_rule(self):
        self.assertNotIn("runs-as-root", codes("FROM a:1\nUSER app\n"))

    def test_user_root_is_still_root(self):
        self.assertIn("runs-as-root", codes("FROM a:1\nUSER root\n"))

    def test_untagged_base_is_flagged(self):
        self.assertIn("unpinned-base", codes("FROM python\nUSER app\n"))

    def test_latest_tag_is_flagged(self):
        self.assertIn("unpinned-base", codes("FROM python:latest\nUSER app\n"))

    def test_pinned_tag_is_not_flagged_as_latest(self):
        found = codes("FROM python:3.12\nUSER app\n")
        self.assertNotIn("unpinned-base", found)

    def test_digest_pin_silences_the_info_rule(self):
        found = codes("FROM python:3.12@sha256:" + "a" * 64 + "\nUSER app\n")
        self.assertNotIn("unpinned-base-digest", found)

    def test_credential_env_is_flagged(self):
        found = codes("FROM a:1\nUSER app\nENV API_KEY=abc123def456\n")
        self.assertIn("secret-in-arg", found)

    def test_credential_env_from_variable_is_not_flagged(self):
        found = codes("FROM a:1\nUSER app\nENV API_KEY=$API_KEY\n")
        self.assertNotIn("secret-in-arg", found)

    def test_placeholder_value_is_not_flagged(self):
        found = codes("FROM a:1\nUSER app\nENV PASSWORD=changeme\n")
        self.assertNotIn("secret-in-arg", found)

    def test_curl_pipe_shell_is_an_error(self):
        findings = lint(parse_dockerfile("FROM a:1\nUSER app\nRUN curl x | sh\n"))
        matches = [f for f in findings if f.code == "curl-pipe-shell"]
        self.assertEqual(matches[0].severity, "error")

    def test_wget_pipe_bash_is_flagged(self):
        self.assertIn("curl-pipe-shell",
                      codes("FROM a:1\nUSER app\nRUN wget -qO- x | bash\n"))

    def test_sudo_is_flagged(self):
        self.assertIn("sudo-in-build", codes("FROM a:1\nUSER app\nRUN sudo apt install x\n"))

    def test_apt_install_without_update_is_flagged(self):
        found = codes("FROM a:1\nUSER app\nRUN apt-get install -y curl\n")
        self.assertIn("apt-cache-stale", found)

    def test_apt_update_in_earlier_layer_is_still_stale(self):
        found = codes("FROM a:1\nUSER app\nRUN apt-get update\nRUN apt-get install -y curl\n")
        self.assertIn("apt-cache-stale", found)

    def test_update_and_install_in_one_layer_is_fine(self):
        found = codes(
            "FROM a:1\nUSER app\nRUN apt-get update && apt-get install -y "
            "--no-install-recommends curl && rm -rf /var/lib/apt/lists/*\n")
        self.assertNotIn("apt-cache-stale", found)
        self.assertNotIn("apt-lists-not-removed", found)
        self.assertNotIn("apt-no-install-recommends", found)

    def test_add_local_path_is_flagged(self):
        self.assertIn("add-instead-of-copy", codes("FROM a:1\nUSER app\nADD . /app\n"))

    def test_copy_without_chown_after_non_root_user(self):
        found = codes("FROM a:1\nUSER app\nCOPY . /app\n")
        self.assertIn("copy-without-chown", found)

    def test_copy_with_chown_is_fine(self):
        found = codes("FROM a:1\nUSER app\nCOPY --chown=app:app . /app\n")
        self.assertNotIn("copy-without-chown", found)

    def test_build_tools_in_single_stage_are_flagged(self):
        found = codes("FROM a:1\nUSER app\nRUN apt-get update && apt-get install -y "
                      "--no-install-recommends gcc make && rm -rf /var/lib/apt/lists/*\n")
        self.assertIn("single-stage-build", found)
        self.assertIn("build-tools-in-final", found)

    def test_build_tools_in_builder_stage_are_fine(self):
        text = (
            "FROM a:1 AS builder\n"
            "RUN apt-get update && apt-get install -y --no-install-recommends gcc "
            "&& rm -rf /var/lib/apt/lists/*\n"
            "FROM a:1\nUSER app\n"
        )
        self.assertNotIn("build-tools-in-final", codes(text))

    def test_missing_healthcheck_is_low(self):
        findings = lint(parse_dockerfile("FROM a:1\nUSER app\n"))
        matches = [f for f in findings if f.code == "no-healthcheck"]
        self.assertEqual(matches[0].severity, "low")

    def test_compose_latest_tag_is_flagged(self):
        findings = lint(parse_dockerfile("FROM a:1\nUSER app\n"), "Dockerfile",
                        "services:\n  web:\n    image: nginx:latest\n")
        self.assertIn("compose-latest-tag", [f.code for f in findings])


class ExampleFixtureTests(unittest.TestCase):
    def test_bad_example_reports_planted_problems(self):
        reported = {f.code for f in lint(parse_dockerfile(read(BAD), BAD), BAD)}
        for expected in (
            "runs-as-root",
            "unpinned-base",
            "secret-in-arg",
            "curl-pipe-shell",
            "apt-cache-stale",
            "add-instead-of-copy",
            "build-tools-in-final",
        ):
            self.assertIn(expected, reported, expected)

    def test_good_example_is_clean_at_high(self):
        findings = lint(parse_dockerfile(read(GOOD), GOOD), GOOD)
        high = [f for f in findings if severity_at_least(f.severity, "high")]
        self.assertEqual(high, [], [f"{f.code}: {f.message}" for f in high])

    def test_findings_sorted_by_severity(self):
        findings = lint(parse_dockerfile(read(BAD), BAD), BAD)
        order = {"info": 0, "low": 1, "medium": 2, "high": 3, "error": 4}
        ranks = [order[f.severity] for f in findings]
        self.assertEqual(ranks, sorted(ranks, reverse=True))

    def test_empty_input_has_no_findings(self):
        self.assertEqual(lint([]), [])


if __name__ == "__main__":
    unittest.main()
