import json
from fastapi.testclient import TestClient
from app.models import DistributionItem, Job
from test_web import _app_with_current_demo_deadline


class EditorAI:
    def __init__(self, callback=None): self.callback=callback
    def call(self,prompt):
        if self.callback: self.callback()
        return json.dumps({"career_advice":"请整理与岗位相关的实习经历。"})
    def complete_text(self,session,prompt): return self.call(prompt)
    def freeze_text_request(self,session): return self.call


def setup_draft():
    app=_app_with_current_demo_deadline()
    client=TestClient(app)
    client.post("/jobs/1/review",data={"action":"approve","note":"测试"})
    client.post("/jobs/1/distribution")
    return app,client


def test_legacy_ai_refinement_does_not_replace_manual_content():
    app,client=setup_draft()
    app.state.ai_settings_service=EditorAI()
    with app.state.session_factory() as session:
        item=session.get(DistributionItem,1)
        item.is_manually_edited=True
        item.ai_content_json='{"career_advice":"人工原稿"}'
        session.commit()
    client.post("/distribution/1/wechat/ai-content",follow_redirects=False)
    with app.state.session_factory() as session:
        assert json.loads(session.get(DistributionItem,1).ai_content_json)["career_advice"]=="人工原稿"


def test_late_legacy_ai_response_does_not_write_changed_job():
    app,client=setup_draft()
    def change():
        with app.state.session_factory() as session:
            session.get(Job,1).version+=1
            session.commit()
    app.state.ai_settings_service=EditorAI(change)
    client.post("/distribution/1/wechat/ai-content",follow_redirects=False)
    with app.state.session_factory() as session:
        assert session.get(DistributionItem,1).ai_content_json==""


def test_ai_refinement_advances_copy_payload_version():
    app,client=setup_draft()
    app.state.ai_settings_service=EditorAI()
    with app.state.session_factory() as session:
        before=session.get(DistributionItem,1).row_version
    client.post("/distribution/1/wechat/ai-content",follow_redirects=False)
    with app.state.session_factory() as session:
        assert session.get(DistributionItem,1).row_version==before+1


def test_late_legacy_ai_response_does_not_overwrite_item_edited_during_request():
    app,client=setup_draft()
    expected={}
    def human_edit_during_network():
        with app.state.session_factory() as session:
            item=session.get(DistributionItem,1)
            item.row_version+=1
            item.is_manually_edited=True
            item.content="网络请求期间人工修改的基础正文"
            item.ai_content_json='{"career_advice":"网络请求期间保存的人工原稿"}'
            item.ai_content_status="人工已修改"
            item.ai_content_error="人工备注保留"
            session.commit()
            expected.update({name:getattr(item,name) for name in (
                "row_version","is_manually_edited","content","ai_content_json","ai_content_status","ai_content_error")})
    app.state.ai_settings_service=EditorAI(human_edit_during_network)
    response=client.post("/distribution/1/wechat/ai-content",follow_redirects=False)
    assert response.status_code==303 and "ai_feedback=error" in response.headers["location"]
    assert expected, "the fake network callback must execute before checking the guard"
    with app.state.session_factory() as session:
        item=session.get(DistributionItem,1)
        assert {name:getattr(item,name) for name in expected}==expected
