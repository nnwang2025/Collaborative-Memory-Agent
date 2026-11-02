BEGIN TRANSACTION;
CREATE TABLE conflict_links (
      conflict_id TEXT PRIMARY KEY, decision_id TEXT NOT NULL REFERENCES decisions,
      left_version_id TEXT REFERENCES decision_versions, right_version_id TEXT REFERENCES decision_versions,
      left_proposal_id TEXT REFERENCES review_proposals, right_proposal_id TEXT REFERENCES review_proposals,
      conflict_description TEXT NOT NULL, resolution_status TEXT NOT NULL DEFAULT 'open',
      reviewer_id TEXT, reviewed_at TEXT, review_note TEXT, created_at TEXT NOT NULL);
INSERT INTO "conflict_links" VALUES('conf_3182d19532e7f4a70f20967821b520c5','dec_486fc2db944c219a5a44bf52ae040c03','dv_3c0c1c993afe96c23dd83f9dd2d81b1f','dv_d46c1881b3b9a911a39e1f1a4efc1927',NULL,'prop_8bfa8224d353f02bb6b295e300bfbe21','候选与当前确认正文不同，需人工判断变化关系。','reviewed_acceptance','synthetic-scenario-author','2025-03-20T00:00:00+00:00','Scripted corpus history, not a human benchmark annotation','2025-02-15T00:00:00+00:00');
INSERT INTO "conflict_links" VALUES('conf_7997d0d1590328b095b7922224141b0f','dec_bd7c458b1b4a11fc9f7db54ba85c8c74','dv_7373e0dc9719faa5376ce9e51da65cc9','dv_f69943d9d56ebc090fc63d2f0e9ae671',NULL,'prop_5e185b171d6b214e96c32b21014dd8ac','候选与当前确认正文不同，需人工判断变化关系。','reviewed_acceptance','synthetic-scenario-author','2025-03-20T00:00:00+00:00','Scripted corpus history, not a human benchmark annotation','2025-02-15T00:00:00+00:00');
CREATE TABLE decision_evidence (
      version_id TEXT NOT NULL REFERENCES decision_versions, evidence_id TEXT NOT NULL, content_hash TEXT NOT NULL,
      relation_type TEXT NOT NULL CHECK(relation_type IN ('supports','contradicts')),
      PRIMARY KEY(version_id,evidence_id,content_hash,relation_type));
INSERT INTO "decision_evidence" VALUES('dv_3c0c1c993afe96c23dd83f9dd2d81b1f','ev_040364740331b53779419de0bb315a9c06aa4ef378aee78b41b9a90adbd5fe0a','34bb07cc8fe528b325074f3ca95612036cf533c66e1ee6b00f46b53e91c676d0','supports');
INSERT INTO "decision_evidence" VALUES('dv_d46c1881b3b9a911a39e1f1a4efc1927','ev_e8a6134474085c010b751f866e005349c83c61801f71fca1fe0281d8c3273295','cb389f60a412f364ddd8e195a62044ab08ff08c4a823ddaecf604cfede56bb15','supports');
INSERT INTO "decision_evidence" VALUES('dv_7373e0dc9719faa5376ce9e51da65cc9','ev_eded2531bd98d9404d9f8911d1e448dc0dc1e36c93d76a8f229fff6136dee19f','20c7d7013aab999f842d351790cc674e729b469db27fc98c86567bd79da32a30','supports');
INSERT INTO "decision_evidence" VALUES('dv_f69943d9d56ebc090fc63d2f0e9ae671','ev_c5d39dfe67531909eb7d327ed92982d03f9a56237584ef107fbdce8e923a4c71','68c255aea4a6b5bb481be8e201394db7767f2161f16bc28b2060cc1d89108d38','supports');
CREATE TABLE decision_review_history (
      seq INTEGER PRIMARY KEY, proposal_id TEXT NOT NULL REFERENCES review_proposals, decision_id TEXT NOT NULL REFERENCES decisions,
      operation TEXT NOT NULL, before_revision INTEGER NOT NULL, after_revision INTEGER NOT NULL,
      before_version_id TEXT, after_version_id TEXT, reviewer_id TEXT NOT NULL, reviewed_at TEXT NOT NULL, review_note TEXT NOT NULL);
INSERT INTO "decision_review_history" VALUES(1,'prop_f143f8df0c6b87d72c2c2909da98138f','dec_486fc2db944c219a5a44bf52ae040c03','accept',0,1,NULL,'dv_3c0c1c993afe96c23dd83f9dd2d81b1f','synthetic-scenario-author','2025-01-10T00:00:00+00:00','Scripted corpus history, not a human benchmark annotation');
INSERT INTO "decision_review_history" VALUES(2,'prop_8bfa8224d353f02bb6b295e300bfbe21','dec_486fc2db944c219a5a44bf52ae040c03','accept',1,2,'dv_3c0c1c993afe96c23dd83f9dd2d81b1f','dv_d46c1881b3b9a911a39e1f1a4efc1927','synthetic-scenario-author','2025-03-20T00:00:00+00:00','Scripted corpus history, not a human benchmark annotation');
INSERT INTO "decision_review_history" VALUES(3,'prop_8212cbd826795565c1a836283184f4a1','dec_bd7c458b1b4a11fc9f7db54ba85c8c74','accept',0,1,NULL,'dv_7373e0dc9719faa5376ce9e51da65cc9','synthetic-scenario-author','2025-01-10T00:00:00+00:00','Scripted corpus history, not a human benchmark annotation');
INSERT INTO "decision_review_history" VALUES(4,'prop_5e185b171d6b214e96c32b21014dd8ac','dec_bd7c458b1b4a11fc9f7db54ba85c8c74','accept',1,2,'dv_7373e0dc9719faa5376ce9e51da65cc9','dv_f69943d9d56ebc090fc63d2f0e9ae671','synthetic-scenario-author','2025-03-20T00:00:00+00:00','Scripted corpus history, not a human benchmark annotation');
CREATE TABLE decision_versions (
      version_id TEXT PRIMARY KEY, decision_id TEXT NOT NULL REFERENCES decisions, revision INTEGER NOT NULL,
      statement TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('proposed','confirmed','superseded','retracted','unknown')),
      event_time TEXT, recorded_at TEXT NOT NULL, supersedes_version_id TEXT REFERENCES decision_versions,
      reviewer_id TEXT NOT NULL, reviewed_at TEXT NOT NULL, proposal_id TEXT NOT NULL UNIQUE REFERENCES review_proposals,
      UNIQUE(decision_id,revision));
INSERT INTO "decision_versions" VALUES('dv_3c0c1c993afe96c23dd83f9dd2d81b1f','dec_486fc2db944c219a5a44bf52ae040c03',1,'采用固定分区归属 B','superseded','2025-01-01T00:00:00+00:00','2025-01-10T00:00:00+00:00',NULL,'synthetic-scenario-author','2025-01-10T00:00:00+00:00','prop_f143f8df0c6b87d72c2c2909da98138f');
INSERT INTO "decision_versions" VALUES('dv_d46c1881b3b9a911a39e1f1a4efc1927','dec_486fc2db944c219a5a44bf52ae040c03',2,'采用带 fencing token 的租约迁移 C','confirmed','2025-02-10T00:00:00+00:00','2025-03-20T00:00:00+00:00','dv_3c0c1c993afe96c23dd83f9dd2d81b1f','synthetic-scenario-author','2025-03-20T00:00:00+00:00','prop_8bfa8224d353f02bb6b295e300bfbe21');
INSERT INTO "decision_versions" VALUES('dv_7373e0dc9719faa5376ce9e51da65cc9','dec_bd7c458b1b4a11fc9f7db54ba85c8c74',1,'采用本地副本 B','superseded','2025-01-01T00:00:00+00:00','2025-01-10T00:00:00+00:00',NULL,'synthetic-scenario-author','2025-01-10T00:00:00+00:00','prop_8212cbd826795565c1a836283184f4a1');
INSERT INTO "decision_versions" VALUES('dv_f69943d9d56ebc090fc63d2f0e9ae671','dec_bd7c458b1b4a11fc9f7db54ba85c8c74',2,'采用版本水位读取 C','confirmed','2025-02-01T00:00:00+00:00','2025-03-20T00:00:00+00:00','dv_7373e0dc9719faa5376ce9e51da65cc9','synthetic-scenario-author','2025-03-20T00:00:00+00:00','prop_5e185b171d6b214e96c32b21014dd8ac');
CREATE TABLE decisions (
      decision_id TEXT PRIMARY KEY, project_id TEXT NOT NULL, dataset_kind TEXT NOT NULL CHECK(dataset_kind IN ('fixture','live')),
      title TEXT NOT NULL, created_at TEXT NOT NULL, current_confirmed_version_id TEXT, revision INTEGER NOT NULL DEFAULT 0);
INSERT INTO "decisions" VALUES('dec_486fc2db944c219a5a44bf52ae040c03','bench-atlas','live','Atlas 调度方案','2025-01-10T00:00:00+00:00','dv_d46c1881b3b9a911a39e1f1a4efc1927',2);
INSERT INTO "decisions" VALUES('dec_bd7c458b1b4a11fc9f7db54ba85c8c74','bench-zephyr','live','Zephyr 缓存方案','2025-01-10T00:00:00+00:00','dv_f69943d9d56ebc090fc63d2f0e9ae671',2);
INSERT INTO "decisions" VALUES('dec_10b4be9a75199ef6259dea0b25f95f91','bench-zephyr','live','Zephyr 清理策略','2025-03-20T00:00:00+00:00',NULL,0);
CREATE TABLE proposal_observations (
      proposal_id TEXT NOT NULL REFERENCES review_proposals, observation_key TEXT NOT NULL,
      source_run_id TEXT NOT NULL, context_json TEXT NOT NULL, risk_flags TEXT NOT NULL, recorded_at TEXT NOT NULL,
      PRIMARY KEY(proposal_id,observation_key));
INSERT INTO "proposal_observations" VALUES('prop_f143f8df0c6b87d72c2c2909da98138f','ae09b61506f45db058ca08c6db00b96e3c2b053ec7da99eb997aa4907b1ea7b0','frozen-authoring-run','{"termination_reason": "sufficient", "validation_level": "identity_and_extract_only", "gaps": [], "conflicts": []}','["identity_only_not_semantically_verified"]','2025-01-10T00:00:00+00:00');
INSERT INTO "proposal_observations" VALUES('prop_8bfa8224d353f02bb6b295e300bfbe21','ae09b61506f45db058ca08c6db00b96e3c2b053ec7da99eb997aa4907b1ea7b0','frozen-authoring-run','{"termination_reason": "sufficient", "validation_level": "identity_and_extract_only", "gaps": [], "conflicts": []}','["identity_only_not_semantically_verified"]','2025-02-15T00:00:00+00:00');
INSERT INTO "proposal_observations" VALUES('prop_8212cbd826795565c1a836283184f4a1','ae09b61506f45db058ca08c6db00b96e3c2b053ec7da99eb997aa4907b1ea7b0','frozen-authoring-run','{"termination_reason": "sufficient", "validation_level": "identity_and_extract_only", "gaps": [], "conflicts": []}','["identity_only_not_semantically_verified"]','2025-01-10T00:00:00+00:00');
INSERT INTO "proposal_observations" VALUES('prop_5e185b171d6b214e96c32b21014dd8ac','ae09b61506f45db058ca08c6db00b96e3c2b053ec7da99eb997aa4907b1ea7b0','frozen-authoring-run','{"termination_reason": "sufficient", "validation_level": "identity_and_extract_only", "gaps": [], "conflicts": []}','["identity_only_not_semantically_verified"]','2025-02-15T00:00:00+00:00');
INSERT INTO "proposal_observations" VALUES('prop_3959fd70901a59471bb51ce2ea5c67b8','ae09b61506f45db058ca08c6db00b96e3c2b053ec7da99eb997aa4907b1ea7b0','frozen-authoring-run','{"termination_reason": "sufficient", "validation_level": "identity_and_extract_only", "gaps": [], "conflicts": []}','["identity_only_not_semantically_verified"]','2025-03-20T00:00:00+00:00');
CREATE TABLE review_proposals (
      proposal_id TEXT PRIMARY KEY, decision_id TEXT NOT NULL REFERENCES decisions, proposed_statement TEXT NOT NULL,
      proposed_event_time TEXT, evidence_refs TEXT NOT NULL, source_run_id TEXT NOT NULL,
      review_status TEXT NOT NULL CHECK(review_status IN ('pending','accepted','rejected')), created_at TEXT NOT NULL,
      reviewed_at TEXT, reviewer_id TEXT, review_note TEXT, risk_flags TEXT NOT NULL, base_revision INTEGER NOT NULL,
      dedupe_key TEXT NOT NULL UNIQUE, investigation_context TEXT NOT NULL);
INSERT INTO "review_proposals" VALUES('prop_f143f8df0c6b87d72c2c2909da98138f','dec_486fc2db944c219a5a44bf52ae040c03','采用固定分区归属 B','2025-01-01T00:00:00+00:00','[{"content_hash": "34bb07cc8fe528b325074f3ca95612036cf533c66e1ee6b00f46b53e91c676d0", "evidence_id": "ev_040364740331b53779419de0bb315a9c06aa4ef378aee78b41b9a90adbd5fe0a", "relation_type": "supports"}]','frozen-authoring-run','accepted','2025-01-10T00:00:00+00:00','2025-01-10T00:00:00+00:00','synthetic-scenario-author','Scripted corpus history, not a human benchmark annotation','["identity_only_not_semantically_verified"]',0,'82b865b5dcfb83e74c0ffd6129316e8190ed951a3a6a9c556cc55cc873bc1a31','{"termination_reason": "sufficient", "validation_level": "identity_and_extract_only", "gaps": [], "conflicts": []}');
INSERT INTO "review_proposals" VALUES('prop_8bfa8224d353f02bb6b295e300bfbe21','dec_486fc2db944c219a5a44bf52ae040c03','采用带 fencing token 的租约迁移 C','2025-02-10T00:00:00+00:00','[{"content_hash": "cb389f60a412f364ddd8e195a62044ab08ff08c4a823ddaecf604cfede56bb15", "evidence_id": "ev_e8a6134474085c010b751f866e005349c83c61801f71fca1fe0281d8c3273295", "relation_type": "supports"}]','frozen-authoring-run','accepted','2025-02-15T00:00:00+00:00','2025-03-20T00:00:00+00:00','synthetic-scenario-author','Scripted corpus history, not a human benchmark annotation','["identity_only_not_semantically_verified"]',1,'2efac420ce75a7ca7ad867c49f2954e90c81bd59f6cdd6397a357eb2870f136a','{"termination_reason": "sufficient", "validation_level": "identity_and_extract_only", "gaps": [], "conflicts": []}');
INSERT INTO "review_proposals" VALUES('prop_8212cbd826795565c1a836283184f4a1','dec_bd7c458b1b4a11fc9f7db54ba85c8c74','采用本地副本 B','2025-01-01T00:00:00+00:00','[{"content_hash": "20c7d7013aab999f842d351790cc674e729b469db27fc98c86567bd79da32a30", "evidence_id": "ev_eded2531bd98d9404d9f8911d1e448dc0dc1e36c93d76a8f229fff6136dee19f", "relation_type": "supports"}]','frozen-authoring-run','accepted','2025-01-10T00:00:00+00:00','2025-01-10T00:00:00+00:00','synthetic-scenario-author','Scripted corpus history, not a human benchmark annotation','["identity_only_not_semantically_verified"]',0,'7f9a66c55eec77c203ddc4ffd9e3959c5a4bdbc83cb32e7d8ef08f3cf791cfef','{"termination_reason": "sufficient", "validation_level": "identity_and_extract_only", "gaps": [], "conflicts": []}');
INSERT INTO "review_proposals" VALUES('prop_5e185b171d6b214e96c32b21014dd8ac','dec_bd7c458b1b4a11fc9f7db54ba85c8c74','采用版本水位读取 C','2025-02-01T00:00:00+00:00','[{"content_hash": "68c255aea4a6b5bb481be8e201394db7767f2161f16bc28b2060cc1d89108d38", "evidence_id": "ev_c5d39dfe67531909eb7d327ed92982d03f9a56237584ef107fbdce8e923a4c71", "relation_type": "supports"}]','frozen-authoring-run','accepted','2025-02-15T00:00:00+00:00','2025-03-20T00:00:00+00:00','synthetic-scenario-author','Scripted corpus history, not a human benchmark annotation','["identity_only_not_semantically_verified"]',1,'dd3c8d1fabbe6ac0eb4eec74cef55d0c4d396cc8bf1664741b44cfa9f14ba8e6','{"termination_reason": "sufficient", "validation_level": "identity_and_extract_only", "gaps": [], "conflicts": []}');
INSERT INTO "review_proposals" VALUES('prop_3959fd70901a59471bb51ce2ea5c67b8','dec_10b4be9a75199ef6259dea0b25f95f91','七天保留窗口','2025-05-01T00:00:00+00:00','[{"content_hash": "5b57a8370d76a7a305116cd240d41ed2832a084f71850b7ef1314ec4226aac9d", "evidence_id": "ev_0387cc6cec469e390ba9a6792c3f9a995525e2acf9a1c4a6dbee431c87ebdb7e", "relation_type": "supports"}]','frozen-authoring-run','pending','2025-03-20T00:00:00+00:00',NULL,NULL,NULL,'["identity_only_not_semantically_verified"]',0,'27835feea80c0a81d0819e6e3e9893a82ff39a9590a2d4679ac98b60427176a7','{"termination_reason": "sufficient", "validation_level": "identity_and_extract_only", "gaps": [], "conflicts": []}');
CREATE UNIQUE INDEX one_confirmed_version ON decision_versions(decision_id) WHERE status='confirmed';
CREATE TRIGGER immutable_version_body BEFORE UPDATE OF statement,event_time,recorded_at,decision_id,
      revision,supersedes_version_id,reviewer_id,reviewed_at,proposal_id ON decision_versions
      BEGIN SELECT RAISE(ABORT,'Version bodies and provenance are immutable'); END;
CREATE TRIGGER no_delete_versions BEFORE DELETE ON decision_versions
      BEGIN SELECT RAISE(ABORT,'Versions cannot be deleted'); END;
CREATE TRIGGER immutable_proposal_body BEFORE UPDATE OF decision_id,proposed_statement,
      proposed_event_time,evidence_refs,source_run_id,created_at,risk_flags,base_revision,dedupe_key,investigation_context ON review_proposals
      BEGIN SELECT RAISE(ABORT,'Proposal bodies are immutable'); END;
CREATE TRIGGER immutable_decision_evidence BEFORE UPDATE ON decision_evidence
      BEGIN SELECT RAISE(ABORT,'Evidence links are immutable'); END;
CREATE TRIGGER no_delete_decision_evidence BEFORE DELETE ON decision_evidence
      BEGIN SELECT RAISE(ABORT,'Evidence links cannot be deleted'); END;
CREATE TRIGGER no_update_review_history BEFORE UPDATE ON decision_review_history
      BEGIN SELECT RAISE(ABORT,'Review history is append-only'); END;
CREATE TRIGGER no_delete_review_history BEFORE DELETE ON decision_review_history
      BEGIN SELECT RAISE(ABORT,'Review history cannot be deleted'); END;
COMMIT;
