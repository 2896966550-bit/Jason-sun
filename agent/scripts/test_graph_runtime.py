"""Graph lifecycle failures must have a single final and must not become memory."""
import unittest
from unittest.mock import Mock, patch
import graph.runner as runner


class GraphRuntimeTest(unittest.TestCase):
    def tiny_graph(self):
        from langgraph.graph import StateGraph, START, END
        from langgraph.checkpoint.memory import InMemorySaver
        saver=InMemorySaver()
        graph=StateGraph(dict)
        graph.add_node('answer',lambda state:{'final':'fixture answer'})
        graph.add_edge(START,'answer');graph.add_edge('answer',END)
        return graph.compile(checkpointer=saver),saver

    def test_single_request_checkpoints_are_released_for_invoke_and_stream(self):
        graph,saver=self.tiny_graph()
        with patch.object(runner,'get_graph',return_value=graph), \
             patch('observability.trace.start_run',return_value=1), \
             patch('observability.trace.end_run'),patch.object(runner,'_persist_memory'):
            runner.run('fixture')
            list(runner.stream_run('fixture'))
        self.assertFalse(saver.storage)
        self.assertFalse(saver.writes)
        self.assertFalse(saver.blobs)

    def test_explicit_caller_thread_preserves_checkpoints(self):
        graph,saver=self.tiny_graph()
        with patch.object(runner,'get_graph',return_value=graph), \
             patch('observability.trace.start_run',return_value=1), \
             patch('observability.trace.end_run'),patch.object(runner,'_persist_memory'):
            runner.run('fixture',thread_id='caller-owned-session')
        self.assertIn('caller-owned-session',saver.storage)

    def test_build_failure_is_a_single_error_event(self):
        with patch.object(runner,'get_graph',side_effect=RuntimeError('fixture build failure')), \
             patch('compliance.gate.audit_log'), patch.object(runner,'_persist_memory') as memory:
            events=list(runner.stream_run('fixture'))
        self.assertEqual(len(events),1)
        self.assertTrue(events[0]['error'])
        memory.assert_not_called()

    def test_cancel_closes_graph_iterator_before_releasing_checkpoints(self):
        order=[]
        def snapshots(*args,**kwargs):
            try:
                yield {'compute_result':'fixture'}
            finally:
                order.append('closed')
        graph=Mock();graph.stream.side_effect=snapshots
        graph.checkpointer.delete_thread.side_effect=lambda thread:order.append('released')
        with patch.object(runner,'get_graph',return_value=graph), \
             patch('observability.trace.start_run',return_value=1),patch('observability.trace.end_run'):
            stream=runner.stream_run('fixture');next(stream);stream.close()
        self.assertEqual(order,['closed','released'])

    def test_stream_failure_never_emits_second_final_or_saves_partial_memory(self):
        def fail(*args,**kwargs):
            yield {'compute_result':'partial fixture'}
            raise RuntimeError('fixture failure')
        graph=Mock();graph.stream.side_effect=fail
        with patch.object(runner,'get_graph',return_value=graph), \
             patch('observability.trace.start_run',return_value=1), \
             patch('observability.trace.end_run') as finish, \
             patch('compliance.gate.audit_log'), patch.object(runner,'_persist_memory') as memory:
            events=list(runner.stream_run('fixture'))
        finals=[e for e in events if 'final' in e]
        self.assertEqual(len(finals),1)
        self.assertTrue(finals[0]['error'])
        self.assertFalse(finish.call_args.args[-1])
        memory.assert_not_called()

    def test_closed_stream_is_traced_as_incomplete(self):
        graph=Mock();graph.stream.return_value=iter([{'compute_result':'first'},{'final':'second'}])
        with patch.object(runner,'get_graph',return_value=graph), \
             patch('observability.trace.start_run',return_value=1), \
             patch('observability.trace.end_run') as finish, patch.object(runner,'_persist_memory') as memory:
            stream=runner.stream_run('fixture');next(stream);stream.close()
        self.assertFalse(finish.call_args.args[-1])
        memory.assert_not_called()
