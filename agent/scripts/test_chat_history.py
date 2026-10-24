"""History pairing contracts, always against a temporary SQLite database."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import chat_history as history


class ChatHistoryTest(unittest.TestCase):
    def setUp(self):
        directory=tempfile.TemporaryDirectory(prefix='equis_history_')
        self.addCleanup(directory.cleanup)
        target=patch.object(history,'DB_PATH',str(Path(directory.name)/'history.db'))
        target.start();self.addCleanup(target.stop)

    def seed(self,*rows):
        for role,content in rows: history.save_message(role,content)

    def test_missing_answer_never_borrows_next_turn(self):
        self.seed(('user','unfinished'),('user','next question'),('assistant','next answer'))
        self.assertEqual(history.get_full_conversation('unfinished'),[('unfinished','')])
        self.assertEqual(history.get_full_conversation('next question'),[('next question','next answer')])

    def test_repeated_question_uses_latest_answer(self):
        self.seed(('user','same'),('assistant','first'),('user','same'),('assistant','latest'))
        self.assertEqual(history.get_full_conversation('same'),[('same','latest')])
        self.assertEqual([m['content'] for m in history.get_recent_pairs(1)],['same','latest'])

    def test_recent_turns_retain_repeats_and_chronology(self):
        self.seed(('user','same'),('assistant','first'),('user','same'),('assistant','latest'))
        self.assertEqual([m['content'] for m in history.get_recent_pairs(2)],['same','first','same','latest'])

    def test_orphan_answers_are_ignored_and_pending_turns_retained(self):
        self.seed(('assistant','orphan'),('user','a'),('user','b'),('assistant','answer b'),('assistant','extra'))
        self.assertEqual([m['content'] for m in history.get_recent_pairs(2)],['a','b','answer b'])

    def test_zero_negative_and_empty_history(self):
        self.assertEqual(history.get_full_conversation('missing'),[])
        self.assertEqual(history.get_recent_pairs(2),[])
        self.seed(('user','a'),('assistant','b'))
        self.assertEqual(history.get_recent_pairs(0),[])
        self.assertEqual(history.get_recent_pairs(-1),[])

    def test_recent_context_limits_response_length(self):
        self.seed(('user','a'),('assistant','x'*400))
        self.assertEqual(len(history.get_recent_pairs(1)[-1]['content']),300)
