// SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
// SPDX-License-Identifier: Apache-2.0

// Generated from actual `codenib guard check --json` runs at each SWE-bench base
// commit with the agent's patch applied. Patches and broken tests: PatchDiff
// (Wang, Pradel and Liu, ICSE 2026), CC BY 4.0. Edit the generator, not this file.

export type GuardHop = { symbol: string; file: string | null; line: number | null; call_line: number | null };
export type GuardCase = {
  id: string; instance: string; agent: string; fixUrl: string; baseCommit: string;
  changed: string[]; newFiles: string[]; flaggedCount: number; totalTests: number;
  tests: { id: string; depth: number; path: GuardHop[] }[];
  broken: { test: string; flagged: boolean; depth: number | null; path: GuardHop[] | null }[];
  note: string; patch: string;
};

export const GUARD_CASES: GuardCase[] = [
 {
  "id": "sympy__sympy-24443-openhands",
  "instance": "sympy/sympy-24443",
  "agent": "OpenHands",
  "fixUrl": "https://github.com/sympy/sympy/pull/24443",
  "baseCommit": "809c53c077485ca48a206cee78340389cb83b7f1",
  "changed": [
   "sympy/combinatorics/homomorphisms.py:_check_homomorphism._image"
  ],
  "newFiles": [],
  "flaggedCount": 3,
  "totalTests": 11144,
  "tests": [
   {
    "id": "sympy/combinatorics/tests/test_homomorphisms.py::test_check_homomorphism",
    "depth": 3,
    "path": [
     {
      "symbol": "sympy/combinatorics/tests/test_homomorphisms.py:test_check_homomorphism",
      "file": "sympy/combinatorics/tests/test_homomorphisms.py",
      "line": 105,
      "call_line": 109
     },
     {
      "symbol": "sympy/combinatorics/homomorphisms.py:homomorphism",
      "file": "sympy/combinatorics/homomorphisms.py",
      "line": 269,
      "call_line": 306
     },
     {
      "symbol": "sympy/combinatorics/homomorphisms.py:_check_homomorphism",
      "file": "sympy/combinatorics/homomorphisms.py",
      "line": 310,
      "call_line": 350
     },
     {
      "symbol": "sympy/combinatorics/homomorphisms.py:_check_homomorphism._image",
      "file": "sympy/combinatorics/homomorphisms.py",
      "line": 318,
      "call_line": null
     }
    ]
   },
   {
    "id": "sympy/combinatorics/tests/test_homomorphisms.py::test_homomorphism",
    "depth": 3,
    "path": [
     {
      "symbol": "sympy/combinatorics/tests/test_homomorphisms.py:test_homomorphism",
      "file": "sympy/combinatorics/tests/test_homomorphisms.py",
      "line": 9,
      "call_line": 17
     },
     {
      "symbol": "sympy/combinatorics/homomorphisms.py:homomorphism",
      "file": "sympy/combinatorics/homomorphisms.py",
      "line": 269,
      "call_line": 306
     },
     {
      "symbol": "sympy/combinatorics/homomorphisms.py:_check_homomorphism",
      "file": "sympy/combinatorics/homomorphisms.py",
      "line": 310,
      "call_line": 350
     },
     {
      "symbol": "sympy/combinatorics/homomorphisms.py:_check_homomorphism._image",
      "file": "sympy/combinatorics/homomorphisms.py",
      "line": 318,
      "call_line": null
     }
    ]
   },
   {
    "id": "sympy/combinatorics/tests/test_homomorphisms.py::test_isomorphisms",
    "depth": 3,
    "path": [
     {
      "symbol": "sympy/combinatorics/tests/test_homomorphisms.py:test_isomorphisms",
      "file": "sympy/combinatorics/tests/test_homomorphisms.py",
      "line": 60,
      "call_line": 74
     },
     {
      "symbol": "sympy/combinatorics/homomorphisms.py:group_isomorphism",
      "file": "sympy/combinatorics/homomorphisms.py",
      "line": 426,
      "call_line": 528
     },
     {
      "symbol": "sympy/combinatorics/homomorphisms.py:_check_homomorphism",
      "file": "sympy/combinatorics/homomorphisms.py",
      "line": 310,
      "call_line": 350
     },
     {
      "symbol": "sympy/combinatorics/homomorphisms.py:_check_homomorphism._image",
      "file": "sympy/combinatorics/homomorphisms.py",
      "line": 318,
      "call_line": null
     }
    ]
   }
  ],
  "broken": [
   {
    "test": "sympy/combinatorics/tests/test_homomorphisms.py::test_check_homomorphism",
    "flagged": true,
    "depth": 3,
    "path": [
     {
      "symbol": "sympy/combinatorics/tests/test_homomorphisms.py:test_check_homomorphism",
      "file": "sympy/combinatorics/tests/test_homomorphisms.py",
      "line": 105,
      "call_line": 109
     },
     {
      "symbol": "sympy/combinatorics/homomorphisms.py:homomorphism",
      "file": "sympy/combinatorics/homomorphisms.py",
      "line": 269,
      "call_line": 306
     },
     {
      "symbol": "sympy/combinatorics/homomorphisms.py:_check_homomorphism",
      "file": "sympy/combinatorics/homomorphisms.py",
      "line": 310,
      "call_line": 350
     },
     {
      "symbol": "sympy/combinatorics/homomorphisms.py:_check_homomorphism._image",
      "file": "sympy/combinatorics/homomorphisms.py",
      "line": 318,
      "call_line": null
     }
    ]
   }
  ],
  "note": "Flagged: the broken test is one of 3 tests the guard listed, three calls from the change.",
  "patch": "diff --git a/sympy/combinatorics/homomorphisms.py b/sympy/combinatorics/homomorphisms.py\nindex bb7bdf32ba..cc91b798fa 100644\n--- a/sympy/combinatorics/homomorphisms.py\n+++ b/sympy/combinatorics/homomorphisms.py\n@@ -333,8 +333,14 @@ def _image(r):\n             # both indices\n             while i < len(r):\n                 power = r_arr[j][1]\n-                if isinstance(domain, PermutationGroup) and r[i] in gens:\n-                    s = domain.generators[gens.index(r[i])]\n+                if isinstance(domain, PermutationGroup):\n+                    # For PermutationGroup, we need to map both original and inverted generators\n+                    # to their corresponding domain generators\n+                    s = r[i]  # Default to r[i] if no match found\n+                    for j, g in enumerate(domain.generators):\n+                        if r[i] == g or r[i] == g**-1:\n+                            s = g  # Use the original generator, not domain.generators[j]\n+                            break\n                 else:\n                     s = r[i]\n                 if s in images:"
 },
 {
  "id": "django__django-11149-codestory",
  "instance": "django/django-11149",
  "agent": "CodeStory",
  "fixUrl": "https://github.com/django/django/pull/11149",
  "baseCommit": "e245046bb6e8b32360aa48b8a41fb7050f0fc730",
  "changed": [
   "django/contrib/admin/options.py:InlineModelAdmin.get_formset",
   "django/contrib/admin/options.py:InlineModelAdmin.has_add_permission",
   "django/contrib/admin/options.py:InlineModelAdmin.has_change_permission",
   "django/contrib/admin/options.py:InlineModelAdmin.has_delete_permission"
  ],
  "newFiles": [
   "reproduce_error.py"
  ],
  "flaggedCount": 4,
  "totalTests": 12143,
  "tests": [
   {
    "id": "tests/modeladmin/tests.py::ModelAdminTests::test_get_fieldsets",
    "depth": 1,
    "path": [
     {
      "symbol": "tests/modeladmin/tests.py:ModelAdminTests.test_get_fieldsets",
      "file": "tests/modeladmin/tests.py",
      "line": 70,
      "call_line": 89
     },
     {
      "symbol": "django/contrib/admin/options.py:InlineModelAdmin.get_formset",
      "file": "django/contrib/admin/options.py",
      "line": 2016,
      "call_line": null
     }
    ]
   },
   {
    "id": "tests/generic_inline_admin/tests.py::GenericInlineModelAdminTest::test_get_fieldsets",
    "depth": 2,
    "path": [
     {
      "symbol": "tests/generic_inline_admin/tests.py:GenericInlineModelAdminTest.test_get_fieldsets",
      "file": "tests/generic_inline_admin/tests.py",
      "line": 387,
      "call_line": 404
     },
     {
      "symbol": "django/contrib/contenttypes/admin.py:GenericInlineModelAdmin.get_formset",
      "file": "django/contrib/contenttypes/admin.py",
      "line": 90,
      "call_line": 101
     },
     {
      "symbol": "django/contrib/admin/options.py:InlineModelAdmin.has_delete_permission",
      "file": "django/contrib/admin/options.py",
      "line": 2132,
      "call_line": null
     }
    ]
   },
   {
    "id": "tests/generic_inline_admin/tests.py::GenericInlineModelAdminTest::test_get_formset_kwargs",
    "depth": 2,
    "path": [
     {
      "symbol": "tests/generic_inline_admin/tests.py:GenericInlineModelAdminTest.test_get_formset_kwargs",
      "file": "tests/generic_inline_admin/tests.py",
      "line": 301,
      "call_line": 305
     },
     {
      "symbol": "django/contrib/contenttypes/admin.py:GenericInlineModelAdmin.get_formset",
      "file": "django/contrib/contenttypes/admin.py",
      "line": 90,
      "call_line": 101
     },
     {
      "symbol": "django/contrib/admin/options.py:InlineModelAdmin.has_delete_permission",
      "file": "django/contrib/admin/options.py",
      "line": 2132,
      "call_line": null
     }
    ]
   },
   {
    "id": "tests/generic_inline_admin/tests.py::NoInlineDeletionTest::test_no_deletion",
    "depth": 2,
    "path": [
     {
      "symbol": "tests/generic_inline_admin/tests.py:NoInlineDeletionTest.test_no_deletion",
      "file": "tests/generic_inline_admin/tests.py",
      "line": 275,
      "call_line": 278
     },
     {
      "symbol": "django/contrib/contenttypes/admin.py:GenericInlineModelAdmin.get_formset",
      "file": "django/contrib/contenttypes/admin.py",
      "line": 90,
      "call_line": 101
     },
     {
      "symbol": "django/contrib/admin/options.py:InlineModelAdmin.has_delete_permission",
      "file": "django/contrib/admin/options.py",
      "line": 2132,
      "call_line": null
     }
    ]
   }
  ],
  "broken": [
   {
    "test": "tests/modeladmin/tests.py::test_get_fieldsets (modeladmin.tests.ModelAdminTests)",
    "flagged": true,
    "depth": 1,
    "path": [
     {
      "symbol": "tests/modeladmin/tests.py:ModelAdminTests.test_get_fieldsets",
      "file": "tests/modeladmin/tests.py",
      "line": 70,
      "call_line": 89
     },
     {
      "symbol": "django/contrib/admin/options.py:InlineModelAdmin.get_formset",
      "file": "django/contrib/admin/options.py",
      "line": 2016,
      "call_line": null
     }
    ]
   }
  ],
  "note": "Flagged: the broken test calls the changed get_formset directly. 4 tests listed out of 12,143.",
  "patch": "diff --git a/django/contrib/admin/options.py b/django/contrib/admin/options.py\nindex 880a2cc17b..0e586364fb 100644\n--- a/django/contrib/admin/options.py\n+++ b/django/contrib/admin/options.py\n@@ -2030,6 +2030,8 @@ class InlineModelAdmin(BaseModelAdmin):\n         # default.\n         exclude = exclude or None\n         can_delete = self.can_delete and self.has_delete_permission(request, obj)\n+        # Set extra=0 if user doesn't have add permission\n+        extra = 0 if not self.has_add_permission(request, obj) else self.get_extra(request, obj, **kwargs)\n         defaults = {\n             'form': self.form,\n             'formset': self.formset,\n@@ -2037,7 +2039,7 @@ class InlineModelAdmin(BaseModelAdmin):\n             'fields': fields,\n             'exclude': exclude,\n             'formfield_callback': partial(self.formfield_for_dbfield, request=request),\n-            'extra': self.get_extra(request, obj, **kwargs),\n+            'extra': extra,\n             'min_num': self.get_min_num(request, obj, **kwargs),\n             'max_num': self.get_max_num(request, obj, **kwargs),\n             'can_delete': can_delete,\n@@ -2113,29 +2115,47 @@ class InlineModelAdmin(BaseModelAdmin):\n \n     def has_add_permission(self, request, obj):\n         if self.opts.auto_created:\n-            # We're checking the rights to an auto-created intermediate model,\n-            # which doesn't have its own individual permissions. The user needs\n-            # to have the view permission for the related model in order to\n-            # be able to do anything with the intermediate model.\n-            return self.has_view_permission(request, obj)\n+            # For auto-created models (like m2m through models), check if user has change\n+            # permission on both related models\n+            opts = self.opts\n+            # Find the target model (the one that's not the parent model)\n+            for field in opts.fields:\n+                if field.remote_field and field.remote_field.model != self.parent_model:\n+                    target_opts = field.remote_field.model._meta\n+                    return (\n+                        request.user.has_perm('%s.%s' % (target_opts.app_label, get_permission_codename('change', target_opts))) and\n+                        request.user.has_perm('%s.%s' % (self.parent_model._meta.app_label, get_permission_codename('change', self.parent_model._meta)))\n+                    )\n         return super().has_add_permission(request)\n \n     def has_change_permission(self, request, obj=None):\n         if self.opts.auto_created:\n-            # We're checking the rights to an auto-created intermediate model,\n-            # which doesn't have its own individual permissions. The user needs\n-            # to have the view permission for the related model in order to\n-            # be able to do anything with the intermediate model.\n-            return self.has_view_permission(request, obj)\n+            # For auto-created models (like m2m through models), check if user has change\n+            # permission on both related models\n+            opts = self.opts\n+            # Find the target model (the one that's not the parent model)\n+            for field in opts.fields:\n+                if field.remote_field and field.remote_field.model != self.parent_model:\n+                    target_opts = field.remote_field.model._meta\n+                    return (\n+                        request.user.has_perm('%s.%s' % (target_opts.app_label, get_permission_codename('change', target_opts))) and\n+                        request.user.has_perm('%s.%s' % (self.parent_model._meta.app_label, get_permission_codename('change', self.parent_model._meta)))\n+                    )\n         return super().has_change_permission(request)\n \n     def has_delete_permission(self, request, obj=None):\n         if self.opts.auto_created:\n-            # We're checking the rights to an auto-created intermediate model,\n-            # which doesn't have its own individual permissions. The user needs\n-            # to have the view permission for the related model in order to\n-            # be able to do anything with the intermediate model.\n-            return self.has_view_permission(request, obj)\n+            # For auto-created models (like m2m through models), check if user has change\n+            # permission on both related models\n+            opts = self.opts\n+            # Find the target model (the one that's not the parent model)\n+            for field in opts.fields:\n+                if field.remote_field and field.remote_field.model != self.parent_model:\n+                    target_opts = field.remote_field.model._meta\n+                    return (\n+                        request.user.has_perm('%s.%s' % (target_opts.app_label, get_permission_codename('change', target_opts))) and\n+                        request.user.has_perm('%s.%s' % (self.parent_model._meta.app_label, get_permission_codename('change', self.parent_model._meta)))\n+                    )\n         return super().has_delete_permission(request, obj)\n \n     def has_view_permission(self, request, obj=None):"
 },
 {
  "id": "django__django-12209-openhands",
  "instance": "django/django-12209",
  "agent": "OpenHands",
  "fixUrl": "https://github.com/django/django/pull/12209",
  "baseCommit": "5a68f024987e6d16c2626a31bf653a2edddea579",
  "changed": [
   "django/db/models/base.py:Model._save_table"
  ],
  "newFiles": [],
  "flaggedCount": 174,
  "totalTests": 12635,
  "tests": [
   {
    "id": "tests/model_inheritance_regress/tests.py::ModelInheritanceTest::test_issue_7276",
    "depth": 2,
    "path": [
     {
      "symbol": "tests/model_inheritance_regress/tests.py:ModelInheritanceTest.test_issue_7276",
      "file": "tests/model_inheritance_regress/tests.py",
      "line": 138,
      "call_line": 143
     },
     {
      "symbol": "django/db/models/base.py:Model.save_base",
      "file": "django/db/models/base.py",
      "line": 750,
      "call_line": 783
     },
     {
      "symbol": "django/db/models/base.py:Model._save_table",
      "file": "django/db/models/base.py",
      "line": 829,
      "call_line": null
     }
    ]
   },
   {
    "id": "tests/model_inheritance_regress/tests.py::ModelInheritanceTest::test_model_inheritance",
    "depth": 2,
    "path": [
     {
      "symbol": "tests/model_inheritance_regress/tests.py:ModelInheritanceTest.test_model_inheritance",
      "file": "tests/model_inheritance_regress/tests.py",
      "line": 21,
      "call_line": 31
     },
     {
      "symbol": "django/db/models/base.py:Model.save_base",
      "file": "django/db/models/base.py",
      "line": 750,
      "call_line": 783
     },
     {
      "symbol": "django/db/models/base.py:Model._save_table",
      "file": "django/db/models/base.py",
      "line": 829,
      "call_line": null
     }
    ]
   },
   {
    "id": "tests/serializers/tests.py::SerializersTestBase::test_serialize_specific_fields",
    "depth": 2,
    "path": [
     {
      "symbol": "tests/serializers/tests.py:SerializersTestBase.test_serialize_specific_fields",
      "file": "tests/serializers/tests.py",
      "line": 146,
      "call_line": 148
     },
     {
      "symbol": "django/db/models/base.py:Model.save_base",
      "file": "django/db/models/base.py",
      "line": 750,
      "call_line": 783
     },
     {
      "symbol": "django/db/models/base.py:Model._save_table",
      "file": "django/db/models/base.py",
      "line": 829,
      "call_line": null
     }
    ]
   },
   {
    "id": "tests/serializers/tests.py::SerializersTestBase::test_serialize_to_stream",
    "depth": 2,
    "path": [
     {
      "symbol": "tests/serializers/tests.py:SerializersTestBase.test_serialize_to_stream",
      "file": "tests/serializers/tests.py",
      "line": 129,
      "call_line": 131
     },
     {
      "symbol": "django/db/models/base.py:Model.save_base",
      "file": "django/db/models/base.py",
      "line": 750,
      "call_line": 783
     },
     {
      "symbol": "django/db/models/base.py:Model._save_table",
      "file": "django/db/models/base.py",
      "line": 829,
      "call_line": null
     }
    ]
   },
   {
    "id": "tests/admin_inlines/tests.py::TestInlineMedia::test_all_inline_media",
    "depth": 3,
    "path": [
     {
      "symbol": "tests/admin_inlines/tests.py:TestInlineMedia.test_all_inline_media",
      "file": "tests/admin_inlines/tests.py",
      "line": 518,
      "call_line": 520
     },
     {
      "symbol": "django/db/models/base.py:Model.save",
      "file": "django/db/models/base.py",
      "line": 663,
      "call_line": 746
     },
     {
      "symbol": "django/db/models/base.py:Model.save_base",
      "file": "django/db/models/base.py",
      "line": 750,
      "call_line": 783
     },
     {
      "symbol": "django/db/models/base.py:Model._save_table",
      "file": "django/db/models/base.py",
      "line": 829,
      "call_line": null
     }
    ]
   },
   {
    "id": "tests/admin_inlines/tests.py::TestInlineMedia::test_inline_media_only_base",
    "depth": 3,
    "path": [
     {
      "symbol": "tests/admin_inlines/tests.py:TestInlineMedia.test_inline_media_only_base",
      "file": "tests/admin_inlines/tests.py",
      "line": 492,
      "call_line": 494
     },
     {
      "symbol": "django/db/models/base.py:Model.save",
      "file": "django/db/models/base.py",
      "line": 663,
      "call_line": 746
     },
     {
      "symbol": "django/db/models/base.py:Model.save_base",
      "file": "django/db/models/base.py",
      "line": 750,
      "call_line": 783
     },
     {
      "symbol": "django/db/models/base.py:Model._save_table",
      "file": "django/db/models/base.py",
      "line": 829,
      "call_line": null
     }
    ]
   },
   {
    "id": "tests/admin_inlines/tests.py::TestInlineMedia::test_inline_media_only_inline",
    "depth": 3,
    "path": [
     {
      "symbol": "tests/admin_inlines/tests.py:TestInlineMedia.test_inline_media_only_inline",
      "file": "tests/admin_inlines/tests.py",
      "line": 500,
      "call_line": 502
     },
     {
      "symbol": "django/db/models/base.py:Model.save",
      "file": "django/db/models/base.py",
      "line": 663,
      "call_line": 746
     },
     {
      "symbol": "django/db/models/base.py:Model.save_base",
      "file": "django/db/models/base.py",
      "line": 750,
      "call_line": 783
     },
     {
      "symbol": "django/db/models/base.py:Model._save_table",
      "file": "django/db/models/base.py",
      "line": 829,
      "call_line": null
     }
    ]
   },
   {
    "id": "tests/admin_views/tests.py::AdminViewStringPrimaryKeyTest::test_shortcut_view_with_escaping",
    "depth": 3,
    "path": [
     {
      "symbol": "tests/admin_views/tests.py:AdminViewStringPrimaryKeyTest.test_shortcut_view_with_escaping",
      "file": "tests/admin_views/tests.py",
      "line": 2847,
      "call_line": 2850
     },
     {
      "symbol": "django/db/models/base.py:Model.save",
      "file": "django/db/models/base.py",
      "line": 663,
      "call_line": 746
     },
     {
      "symbol": "django/db/models/base.py:Model.save_base",
      "file": "django/db/models/base.py",
      "line": 750,
      "call_line": 783
     },
     {
      "symbol": "django/db/models/base.py:Model._save_table",
      "file": "django/db/models/base.py",
      "line": 829,
      "call_line": null
     }
    ]
   },
   {
    "id": "tests/admin_views/tests.py::AdminViewStringPrimaryKeyTest::test_url_conflicts_with_delete",
    "depth": 3,
    "path": [
     {
      "symbol": "tests/admin_views/tests.py:AdminViewStringPrimaryKeyTest.test_url_conflicts_with_delete",
      "file": "tests/admin_views/tests.py",
      "line": 2827,
      "call_line": 2830
     },
     {
      "symbol": "django/db/models/base.py:Model.save",
      "file": "django/db/models/base.py",
      "line": 663,
      "call_line": 746
     },
     {
      "symbol": "django/db/models/base.py:Model.save_base",
      "file": "django/db/models/base.py",
      "line": 750,
      "call_line": 783
     },
     {
      "symbol": "django/db/models/base.py:Model._save_table",
      "file": "django/db/models/base.py",
      "line": 829,
      "call_line": null
     }
    ]
   },
   {
    "id": "tests/admin_views/tests.py::AdminViewStringPrimaryKeyTest::test_url_conflicts_with_history",
    "depth": 3,
    "path": [
     {
      "symbol": "tests/admin_views/tests.py:AdminViewStringPrimaryKeyTest.test_url_conflicts_with_history",
      "file": "tests/admin_views/tests.py",
      "line": 2837,
      "call_line": 2840
     },
     {
      "symbol": "django/db/models/base.py:Model.save",
      "file": "django/db/models/base.py",
      "line": 663,
      "call_line": 746
     },
     {
      "symbol": "django/db/models/base.py:Model.save_base",
      "file": "django/db/models/base.py",
      "line": 750,
      "call_line": 783
     },
     {
      "symbol": "django/db/models/base.py:Model._save_table",
      "file": "django/db/models/base.py",
      "line": 829,
      "call_line": null
     }
    ]
   },
   {
    "id": "tests/annotations/tests.py::NonAggregateAnnotationTestCase::test_custom_functions",
    "depth": 3,
    "path": [
     {
      "symbol": "tests/annotations/tests.py:NonAggregateAnnotationTestCase.test_custom_functions",
      "file": "tests/annotations/tests.py",
      "line": 492,
      "call_line": 494
     },
     {
      "symbol": "django/db/models/base.py:Model.save",
      "file": "django/db/models/base.py",
      "line": 663,
      "call_line": 746
     },
     {
      "symbol": "django/db/models/base.py:Model.save_base",
      "file": "django/db/models/base.py",
      "line": 750,
      "call_line": 783
     },
     {
      "symbol": "django/db/models/base.py:Model._save_table",
      "file": "django/db/models/base.py",
      "line": 829,
      "call_line": null
     }
    ]
   },
   {
    "id": "tests/annotations/tests.py::NonAggregateAnnotationTestCase::test_custom_functions_can_ref_other_functions",
    "depth": 3,
    "path": [
     {
      "symbol": "tests/annotations/tests.py:NonAggregateAnnotationTestCase.test_custom_functions_can_ref_other_functions",
      "file": "tests/annotations/tests.py",
      "line": 519,
      "call_line": 521
     },
     {
      "symbol": "django/db/models/base.py:Model.save",
      "file": "django/db/models/base.py",
      "line": 663,
      "call_line": 746
     },
     {
      "symbol": "django/db/models/base.py:Model.save_base",
      "file": "django/db/models/base.py",
      "line": 750,
      "call_line": 783
     },
     {
      "symbol": "django/db/models/base.py:Model._save_table",
      "file": "django/db/models/base.py",
      "line": 829,
      "call_line": null
     }
    ]
   }
  ],
  "broken": [
   {
    "test": "tests/basic/tests.py::test_save_primary_with_default (basic.tests.ModelInstanceCreationTests)",
    "flagged": true,
    "depth": 3,
    "path": [
     {
      "symbol": "tests/basic/tests.py:ModelInstanceCreationTests.test_save_primary_with_default",
      "file": "tests/basic/tests.py",
      "line": 137,
      "call_line": 140
     },
     {
      "symbol": "django/db/models/base.py:Model.save",
      "file": "django/db/models/base.py",
      "line": 663,
      "call_line": 746
     },
     {
      "symbol": "django/db/models/base.py:Model.save_base",
      "file": "django/db/models/base.py",
      "line": 750,
      "call_line": 783
     },
     {
      "symbol": "django/db/models/base.py:Model._save_table",
      "file": "django/db/models/base.py",
      "line": 829,
      "call_line": null
     }
    ]
   }
  ],
  "note": "Flagged through Model.save, save_base and _save_table. Saving a model is a common path, so the guard lists 174 tests.",
  "patch": "diff --git a/django/db/models/base.py b/django/db/models/base.py\nindex e3b14a41a0..3a4cbfe655 100644\n--- a/django/db/models/base.py\n+++ b/django/db/models/base.py\n@@ -847,12 +847,15 @@ class Model(metaclass=ModelBase):\n         if not pk_set and (force_update or update_fields):\n             raise ValueError(\"Cannot force an update in save() with no primary key.\")\n         updated = False\n-        # Skip an UPDATE when adding an instance and primary key has a default.\n+        # Skip an UPDATE when adding an instance and primary key has a default,\n+        # but only if an explicit pk value was not provided.\n         if (\n             not force_insert and\n             self._state.adding and\n             self._meta.pk.default and\n-            self._meta.pk.default is not NOT_PROVIDED\n+            self._meta.pk.default is not NOT_PROVIDED and\n+            # Don't force insert if an explicit pk value was provided\n+            getattr(self, self._meta.pk.attname) is None\n         ):\n             force_insert = True\n         # If possible, try an UPDATE. If that doesn't update anything, do an INSERT."
 },
 {
  "id": "pydata__xarray-4629-openhands",
  "instance": "pydata/xarray-4629",
  "agent": "OpenHands",
  "fixUrl": "https://github.com/pydata/xarray/pull/4629",
  "baseCommit": "a41edc7bf5302f2ea327943c0c48c532b12009bc",
  "changed": [
   "xarray/core/merge.py",
   "xarray/core/merge.py:merge_attrs"
  ],
  "newFiles": [
   "reproduce_error.py",
   "test_edge_cases.py"
  ],
  "flaggedCount": 1304,
  "totalTests": 2198,
  "tests": [
   {
    "id": "xarray/tests/test_concat.py::TestConcatDataset::test_concat_constant_index",
    "depth": 1,
    "path": [
     {
      "symbol": "xarray/tests/test_concat.py:TestConcatDataset.test_concat_constant_index",
      "file": "xarray/tests/test_concat.py",
      "line": 147,
      "call_line": 155
     },
     {
      "symbol": "xarray/core/merge.py:MergeError",
      "file": "xarray/core/merge.py",
      "line": 73,
      "call_line": null
     }
    ]
   },
   {
    "id": "xarray/tests/test_concat.py::TestConcatDataset::test_concat_coords",
    "depth": 1,
    "path": [
     {
      "symbol": "xarray/tests/test_concat.py:TestConcatDataset.test_concat_coords",
      "file": "xarray/tests/test_concat.py",
      "line": 133,
      "call_line": 144
     },
     {
      "symbol": "xarray/core/merge.py:MergeError",
      "file": "xarray/core/merge.py",
      "line": 73,
      "call_line": null
     }
    ]
   },
   {
    "id": "xarray/tests/test_dask.py::test_lazy_array_equiv_merge",
    "depth": 1,
    "path": [
     {
      "symbol": "xarray/tests/test_dask.py:test_lazy_array_equiv_merge",
      "file": "xarray/tests/test_dask.py",
      "line": 1516,
      "call_line": 1525
     },
     {
      "symbol": "xarray/core/merge.py:merge",
      "file": "xarray/core/merge.py",
      "line": 628,
      "call_line": null
     }
    ]
   },
   {
    "id": "xarray/tests/test_dataarray.py::TestDataArray::test_inplace_math_automatic_alignment",
    "depth": 1,
    "path": [
     {
      "symbol": "xarray/tests/test_dataarray.py:TestDataArray.test_inplace_math_automatic_alignment",
      "file": "xarray/tests/test_dataarray.py",
      "line": 1970,
      "call_line": 1973
     },
     {
      "symbol": "xarray/core/merge.py:MergeError",
      "file": "xarray/core/merge.py",
      "line": 73,
      "call_line": null
     }
    ]
   },
   {
    "id": "xarray/tests/test_dataset.py::TestDataset::test_combine_first",
    "depth": 1,
    "path": [
     {
      "symbol": "xarray/tests/test_dataset.py:TestDataset.test_combine_first",
      "file": "xarray/tests/test_dataset.py",
      "line": 5517,
      "call_line": 5527
     },
     {
      "symbol": "xarray/core/merge.py:merge",
      "file": "xarray/core/merge.py",
      "line": 628,
      "call_line": null
     }
    ]
   },
   {
    "id": "xarray/tests/test_dataset.py::TestDataset::test_constructor_compat",
    "depth": 1,
    "path": [
     {
      "symbol": "xarray/tests/test_dataset.py:TestDataset.test_constructor_compat",
      "file": "xarray/tests/test_dataset.py",
      "line": 509,
      "call_line": 534
     },
     {
      "symbol": "xarray/core/merge.py:MergeError",
      "file": "xarray/core/merge.py",
      "line": 73,
      "call_line": null
     }
    ]
   },
   {
    "id": "xarray/tests/test_dataset.py::TestDataset::test_constructor_invalid_dims",
    "depth": 1,
    "path": [
     {
      "symbol": "xarray/tests/test_dataset.py:TestDataset.test_constructor_invalid_dims",
      "file": "xarray/tests/test_dataset.py",
      "line": 401,
      "call_line": 403
     },
     {
      "symbol": "xarray/core/merge.py:MergeError",
      "file": "xarray/core/merge.py",
      "line": 73,
      "call_line": null
     }
    ]
   },
   {
    "id": "xarray/tests/test_dataset.py::TestDataset::test_coords_merge",
    "depth": 1,
    "path": [
     {
      "symbol": "xarray/tests/test_dataset.py:TestDataset.test_coords_merge",
      "file": "xarray/tests/test_dataset.py",
      "line": 811,
      "call_line": 823
     },
     {
      "symbol": "xarray/core/merge.py:MergeError",
      "file": "xarray/core/merge.py",
      "line": 73,
      "call_line": null
     }
    ]
   },
   {
    "id": "xarray/tests/test_merge.py::TestMergeFunction::test_merge_alignment_error",
    "depth": 1,
    "path": [
     {
      "symbol": "xarray/tests/test_merge.py:TestMergeFunction.test_merge_alignment_error",
      "file": "xarray/tests/test_merge.py",
      "line": 127,
      "call_line": 131
     },
     {
      "symbol": "xarray/core/merge.py:merge",
      "file": "xarray/core/merge.py",
      "line": 628,
      "call_line": null
     }
    ]
   },
   {
    "id": "xarray/tests/test_merge.py::TestMergeFunction::test_merge_arrays",
    "depth": 1,
    "path": [
     {
      "symbol": "xarray/tests/test_merge.py:TestMergeFunction.test_merge_arrays",
      "file": "xarray/tests/test_merge.py",
      "line": 32,
      "call_line": 34
     },
     {
      "symbol": "xarray/core/merge.py:merge",
      "file": "xarray/core/merge.py",
      "line": 628,
      "call_line": null
     }
    ]
   },
   {
    "id": "xarray/tests/test_merge.py::TestMergeFunction::test_merge_arrays_attrs",
    "depth": 1,
    "path": [
     {
      "symbol": "xarray/tests/test_merge.py:TestMergeFunction.test_merge_arrays_attrs",
      "file": "xarray/tests/test_merge.py",
      "line": 66,
      "call_line": 104
     },
     {
      "symbol": "xarray/core/merge.py:MergeError",
      "file": "xarray/core/merge.py",
      "line": 73,
      "call_line": null
     }
    ]
   },
   {
    "id": "xarray/tests/test_merge.py::TestMergeFunction::test_merge_arrays_attrs_default",
    "depth": 1,
    "path": [
     {
      "symbol": "xarray/tests/test_merge.py:TestMergeFunction.test_merge_arrays_attrs_default",
      "file": "xarray/tests/test_merge.py",
      "line": 53,
      "call_line": 61
     },
     {
      "symbol": "xarray/core/merge.py:merge",
      "file": "xarray/core/merge.py",
      "line": 628,
      "call_line": null
     }
    ]
   }
  ],
  "broken": [
   {
    "test": "xarray/tests/test_dataset.py::TestDataset::test_copy",
    "flagged": true,
    "depth": 3,
    "path": [
     {
      "symbol": "xarray/tests/test_dataset.py:TestDataset.test_copy",
      "file": "xarray/tests/test_dataset.py",
      "line": 2427,
      "call_line": 2428
     },
     {
      "symbol": "xarray/tests/test_dataset.py:create_test_data",
      "file": "xarray/tests/test_dataset.py",
      "line": 63,
      "call_line": 72
     },
     {
      "symbol": "xarray/core/dataset.py:Dataset.__init__",
      "file": "xarray/core/dataset.py",
      "line": 605,
      "call_line": 630
     },
     {
      "symbol": "xarray/core/merge.py:merge_data_and_coords",
      "file": "xarray/core/merge.py",
      "line": 462,
      "call_line": null
     }
    ]
   }
  ],
  "note": "Flagged, but by a coarse rule: the patch also adds an import at the top of merge.py, so the guard treats the whole file as changed and lists 1,304 of 2,198 tests. The path ends at merge_data_and_coords, not at the edited merge_attrs.",
  "patch": "diff --git a/xarray/core/merge.py b/xarray/core/merge.py\nindex ca4e29b6..d96092da 100644\n--- a/xarray/core/merge.py\n+++ b/xarray/core/merge.py\n@@ -15,6 +15,7 @@ from typing import (\n     Union,\n )\n \n+import copy\n import pandas as pd\n \n from . import dtypes, pdcompat\n@@ -501,7 +502,7 @@ def merge_attrs(variable_attrs, combine_attrs):\n     if combine_attrs == \"drop\":\n         return {}\n     elif combine_attrs == \"override\":\n-        return variable_attrs[0]\n+        return copy.deepcopy(variable_attrs[0])\n     elif combine_attrs == \"no_conflicts\":\n         result = dict(variable_attrs[0])\n         for attrs in variable_attrs[1:]:"
 },
 {
  "id": "django__django-11292-openhands",
  "instance": "django/django-11292",
  "agent": "OpenHands",
  "fixUrl": "https://github.com/django/django/pull/11292",
  "baseCommit": "eb16c7260e573ec513d84cb586d96bdf508f3173",
  "changed": [
   "django/core/management/base.py:BaseCommand.create_parser"
  ],
  "newFiles": [],
  "flaggedCount": 424,
  "totalTests": 12166,
  "tests": [
   {
    "id": "tests/user_commands/tests.py::CommandTests::test_create_parser_kwargs",
    "depth": 1,
    "path": [
     {
      "symbol": "tests/user_commands/tests.py:CommandTests.test_create_parser_kwargs",
      "file": "tests/user_commands/tests.py",
      "line": 226,
      "call_line": 229
     },
     {
      "symbol": "django/core/management/base.py:BaseCommand.create_parser",
      "file": "django/core/management/base.py",
      "line": 249,
      "call_line": null
     }
    ]
   },
   {
    "id": "tests/admin_scripts/tests.py::CommandTypes::test_base_run_from_argv",
    "depth": 2,
    "path": [
     {
      "symbol": "tests/admin_scripts/tests.py:CommandTypes.test_base_run_from_argv",
      "file": "tests/admin_scripts/tests.py",
      "line": 1620,
      "call_line": 1636
     },
     {
      "symbol": "django/core/management/base.py:BaseCommand.run_from_argv",
      "file": "django/core/management/base.py",
      "line": 306,
      "call_line": 315
     },
     {
      "symbol": "django/core/management/base.py:BaseCommand.create_parser",
      "file": "django/core/management/base.py",
      "line": 249,
      "call_line": null
     }
    ]
   },
   {
    "id": "tests/admin_scripts/tests.py::CommandTypes::test_command_color",
    "depth": 2,
    "path": [
     {
      "symbol": "tests/admin_scripts/tests.py:CommandTypes.test_command_color",
      "file": "tests/admin_scripts/tests.py",
      "line": 1478,
      "call_line": 1482
     },
     {
      "symbol": "django/core/management.py:call_command",
      "file": "django/core/management/__init__.py",
      "line": 75,
      "call_line": 113
     },
     {
      "symbol": "django/core/management/base.py:BaseCommand.create_parser",
      "file": "django/core/management/base.py",
      "line": 249,
      "call_line": null
     }
    ]
   },
   {
    "id": "tests/admin_scripts/tests.py::CommandTypes::test_command_no_color",
    "depth": 2,
    "path": [
     {
      "symbol": "tests/admin_scripts/tests.py:CommandTypes.test_command_no_color",
      "file": "tests/admin_scripts/tests.py",
      "line": 1492,
      "call_line": 1497
     },
     {
      "symbol": "django/core/management.py:call_command",
      "file": "django/core/management/__init__.py",
      "line": 75,
      "call_line": 113
     },
     {
      "symbol": "django/core/management/base.py:BaseCommand.create_parser",
      "file": "django/core/management/base.py",
      "line": 249,
      "call_line": null
     }
    ]
   },
   {
    "id": "tests/admin_scripts/tests.py::CommandTypes::test_custom_stderr",
    "depth": 2,
    "path": [
     {
      "symbol": "tests/admin_scripts/tests.py:CommandTypes.test_custom_stderr",
      "file": "tests/admin_scripts/tests.py",
      "line": 1553,
      "call_line": 1562
     },
     {
      "symbol": "django/core/management.py:call_command",
      "file": "django/core/management/__init__.py",
      "line": 75,
      "call_line": 113
     },
     {
      "symbol": "django/core/management/base.py:BaseCommand.create_parser",
      "file": "django/core/management/base.py",
      "line": 249,
      "call_line": null
     }
    ]
   },
   {
    "id": "tests/admin_scripts/tests.py::CommandTypes::test_custom_stdout",
    "depth": 2,
    "path": [
     {
      "symbol": "tests/admin_scripts/tests.py:CommandTypes.test_custom_stdout",
      "file": "tests/admin_scripts/tests.py",
      "line": 1536,
      "call_line": 1545
     },
     {
      "symbol": "django/core/management.py:call_command",
      "file": "django/core/management/__init__.py",
      "line": 75,
      "call_line": 113
     },
     {
      "symbol": "django/core/management/base.py:BaseCommand.create_parser",
      "file": "django/core/management/base.py",
      "line": 249,
      "call_line": null
     }
    ]
   },
   {
    "id": "tests/admin_scripts/tests.py::CommandTypes::test_force_color_command_init",
    "depth": 2,
    "path": [
     {
      "symbol": "tests/admin_scripts/tests.py:CommandTypes.test_force_color_command_init",
      "file": "tests/admin_scripts/tests.py",
      "line": 1517,
      "call_line": 1522
     },
     {
      "symbol": "django/core/management.py:call_command",
      "file": "django/core/management/__init__.py",
      "line": 75,
      "call_line": 113
     },
     {
      "symbol": "django/core/management/base.py:BaseCommand.create_parser",
      "file": "django/core/management/base.py",
      "line": 249,
      "call_line": null
     }
    ]
   },
   {
    "id": "tests/admin_scripts/tests.py::CommandTypes::test_force_color_execute",
    "depth": 2,
    "path": [
     {
      "symbol": "tests/admin_scripts/tests.py:CommandTypes.test_force_color_execute",
      "file": "tests/admin_scripts/tests.py",
      "line": 1508,
      "call_line": 1513
     },
     {
      "symbol": "django/core/management.py:call_command",
      "file": "django/core/management/__init__.py",
      "line": 75,
      "call_line": 113
     },
     {
      "symbol": "django/core/management/base.py:BaseCommand.create_parser",
      "file": "django/core/management/base.py",
      "line": 249,
      "call_line": null
     }
    ]
   },
   {
    "id": "tests/admin_scripts/tests.py::CommandTypes::test_no_color_force_color_mutually_exclusive_command_init",
    "depth": 2,
    "path": [
     {
      "symbol": "tests/admin_scripts/tests.py:CommandTypes.test_no_color_force_color_mutually_exclusive_command_init",
      "file": "tests/admin_scripts/tests.py",
      "line": 1531,
      "call_line": 1534
     },
     {
      "symbol": "django/core/management.py:call_command",
      "file": "django/core/management/__init__.py",
      "line": 75,
      "call_line": 113
     },
     {
      "symbol": "django/core/management/base.py:BaseCommand.create_parser",
      "file": "django/core/management/base.py",
      "line": 249,
      "call_line": null
     }
    ]
   },
   {
    "id": "tests/admin_scripts/tests.py::CommandTypes::test_no_color_force_color_mutually_exclusive_execute",
    "depth": 2,
    "path": [
     {
      "symbol": "tests/admin_scripts/tests.py:CommandTypes.test_no_color_force_color_mutually_exclusive_execute",
      "file": "tests/admin_scripts/tests.py",
      "line": 1526,
      "call_line": 1529
     },
     {
      "symbol": "django/core/management.py:call_command",
      "file": "django/core/management/__init__.py",
      "line": 75,
      "call_line": 113
     },
     {
      "symbol": "django/core/management/base.py:BaseCommand.create_parser",
      "file": "django/core/management/base.py",
      "line": 249,
      "call_line": null
     }
    ]
   },
   {
    "id": "tests/admin_scripts/tests.py::CommandTypes::test_run_from_argv_closes_connections",
    "depth": 2,
    "path": [
     {
      "symbol": "tests/admin_scripts/tests.py:CommandTypes.test_run_from_argv_closes_connections",
      "file": "tests/admin_scripts/tests.py",
      "line": 1670,
      "call_line": 1679
     },
     {
      "symbol": "django/core/management/base.py:BaseCommand.run_from_argv",
      "file": "django/core/management/base.py",
      "line": 306,
      "call_line": 315
     },
     {
      "symbol": "django/core/management/base.py:BaseCommand.create_parser",
      "file": "django/core/management/base.py",
      "line": 249,
      "call_line": null
     }
    ]
   },
   {
    "id": "tests/admin_scripts/tests.py::CommandTypes::test_run_from_argv_non_ascii_error",
    "depth": 2,
    "path": [
     {
      "symbol": "tests/admin_scripts/tests.py:CommandTypes.test_run_from_argv_non_ascii_error",
      "file": "tests/admin_scripts/tests.py",
      "line": 1656,
      "call_line": 1668
     },
     {
      "symbol": "django/core/management/base.py:BaseCommand.run_from_argv",
      "file": "django/core/management/base.py",
      "line": 306,
      "call_line": 315
     },
     {
      "symbol": "django/core/management/base.py:BaseCommand.create_parser",
      "file": "django/core/management/base.py",
      "line": 249,
      "call_line": null
     }
    ]
   }
  ],
  "broken": [
   {
    "test": "tests/admin_scripts/tests.py::NoArg Commands can be executed",
    "flagged": false,
    "depth": null,
    "path": null
   },
   {
    "test": "tests/admin_scripts/tests.py::User BaseCommands can execute with multiple options when a label is provided",
    "flagged": false,
    "depth": null,
    "path": null
   },
   {
    "test": "tests/admin_scripts/tests.py::alternate: manage.py can execute user commands if settings are provided in environment",
    "flagged": false,
    "depth": null,
    "path": null
   },
   {
    "test": "tests/admin_scripts/tests.py::alternate: manage.py output syntax color can be deactivated with the `--no-color` option",
    "flagged": false,
    "depth": null,
    "path": null
   },
   {
    "test": "tests/admin_scripts/tests.py::User LabelCommands are executed multiple times if multiple labels are provided",
    "flagged": false,
    "depth": null,
    "path": null
   },
   {
    "test": "tests/admin_scripts/tests.py::User AppCommands raise an error when multiple app names are provided",
    "flagged": false,
    "depth": null,
    "path": null
   },
   {
    "test": "tests/admin_scripts/tests.py::User LabelCommands can execute when a label is provided",
    "flagged": false,
    "depth": null,
    "path": null
   },
   {
    "test": "tests/admin_scripts/tests.py::Options are correctly handled when they are passed before and after",
    "flagged": false,
    "depth": null,
    "path": null
   },
   {
    "test": "tests/admin_scripts/tests.py::Options passed after settings are correctly handled.",
    "flagged": false,
    "depth": null,
    "path": null
   },
   {
    "test": "tests/admin_scripts/tests.py::Short options passed before settings are correctly handled.",
    "flagged": false,
    "depth": null,
    "path": null
   },
   {
    "test": "tests/admin_scripts/tests.py::User BaseCommands can execute when no labels are provided",
    "flagged": false,
    "depth": null,
    "path": null
   },
   {
    "test": "tests/admin_scripts/tests.py::Options passed before settings are correctly handled.",
    "flagged": false,
    "depth": null,
    "path": null
   },
   {
    "test": "tests/admin_scripts/tests.py::User BaseCommands can execute with options when a label is provided",
    "flagged": false,
    "depth": null,
    "path": null
   },
   {
    "test": "tests/admin_scripts/tests.py::Short options passed after settings are correctly handled.",
    "flagged": false,
    "depth": null,
    "path": null
   },
   {
    "test": "tests/admin_scripts/tests.py::alternate: manage.py can execute user commands if settings are provided as argument",
    "flagged": false,
    "depth": null,
    "path": null
   },
   {
    "test": "tests/admin_scripts/tests.py::User BaseCommands can execute when a label is provided",
    "flagged": false,
    "depth": null,
    "path": null
   }
  ],
  "note": "Missed: the broken tests start manage.py in a separate process, which a code graph cannot follow. The guard listed 424 other tests that reach the changed parser.",
  "patch": "diff --git a/django/core/management/base.py b/django/core/management/base.py\nindex 0c3a981290..118dc38a5c 100644\n--- a/django/core/management/base.py\n+++ b/django/core/management/base.py\n@@ -286,6 +286,10 @@ class BaseCommand:\n             '--force-color', action='store_true',\n             help='Force colorization of the command output.',\n         )\n+        parser.add_argument(\n+            '--skip-checks', action='store_true',\n+            help='Skip system checks.',\n+        )\n         self.add_arguments(parser)\n         return parser"
 }
];
