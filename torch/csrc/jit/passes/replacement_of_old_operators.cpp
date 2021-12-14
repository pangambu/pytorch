#include <torch/csrc/jit/passes/replacement_of_old_operators.h>

#include <caffe2/serialize/versions.h>
#include <c10/util/Exception.h>
#include <torch/csrc/jit/frontend/schema_matching.h>
#include <torch/csrc/jit/ir/irparser.h>
#include <torch/csrc/jit/operator_upgraders/upgraders.h>
#include <torch/csrc/jit/operator_upgraders/utils.h>
#include <torch/csrc/jit/operator_upgraders/version_map.h>
#include <torch/csrc/jit/runtime/graph_iterator.h>
#include <limits>
#include <regex>
#include <string>

namespace torch {
namespace jit {

struct OldOpsReplacer {
  OldOpsReplacer(std::shared_ptr<Graph> graph) : graph_(std::move(graph)) {}

  void run() {
    if (!graph_->get_op_version().has_value()) {
      return;
    }
    auto current_version = graph_->get_op_version().value();
    DepthFirstGraphNodeIterator graph_it(graph_);
    Node* node = graph_it.next();
    int updated_version = 0;
    while (node) {
      if (auto schema = node->maybeSchema()) {
        auto schema_name = getFullSchemaName(*schema);
        // this implies there was a version bump because of this operator
        auto version_entry = kOperatorVersionMap.find(schema_name);
        if (version_entry != kOperatorVersionMap.end()) {
          const auto& entry = version_entry->second;
          updated_version = std::max(updated_version, entry[entry.size() - 1].bumped_at_version);
          auto upgrader_entry =
              findUpgrader(version_entry->second, current_version);
          if (!upgrader_entry.has_value()) {
            if (!isOpSymbolCurrent(schema_name, current_version)) {
              TORCH_INTERNAL_ASSERT(false, "Upgrader must be present for ", schema_name);
            }
            return;
          }
          auto upgrader_entry_val = upgrader_entry.value();
          auto upgrader_name = upgrader_entry_val.upgrader_name;
          auto upgrader_graph_entry = dump_upgraders_map().find(upgrader_name);
          TORCH_INTERNAL_ASSERT(upgrader_graph_entry != dump_upgraders_map().end(), "Corresponding upgrader graph for ", upgrader_name, " must exist");
          Graph upgrader_graph;
          parseIR(upgrader_graph_entry->second, &upgrader_graph);
          // inline the upgrader function body
          WithInsertPoint guard(node);
          auto new_outputs =
              insertGraph(*node->owningGraph(), upgrader_graph, node->inputs());
          const auto& old_outputs = node->outputs();
          TORCH_INTERNAL_ASSERT(new_outputs.size() == old_outputs.size());
          for (const auto i : c10::irange(old_outputs.size())) {
            TORCH_INTERNAL_ASSERT(
                new_outputs[i]->type() == old_outputs[i]->type())
            old_outputs[i]->replaceAllUsesWith(new_outputs[i]);
          }
          node->removeAllInputs();
          node->destroy();
        }
      }
      node = graph_it.next();
    }

    // now that we updated the graph, we want to bump the
    // graph version too.
    graph_->set_op_version(updated_version);
  }

  std::shared_ptr<Graph> graph_;
};

TORCH_API void ApplyOldOpsUpgraders(std::shared_ptr<Graph> graph) {
  OldOpsReplacer(graph).run();
}

} // namespace jit
} // namespace torch
